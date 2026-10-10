# HTTP API

Base URL: `http://127.0.0.1:3188`. All responses are JSON.

If `HELIOS_LOCAL_API_KEY` is configured, add `Authorization: Bearer <local-secret>` to every endpoint except `/health`. The health response never exposes credentials.

## Health and catalog

```bash
curl http://127.0.0.1:3188/health
curl 'http://127.0.0.1:3188/models?search=gemini&limit=10'
```

`limit` must be an integer from 1 to 200.

## Run one model

```bash
curl -X POST http://127.0.0.1:3188/run \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "glm",
    "prompt": "Summarize this problem and propose a solution.",
    "reasoning_effort": "low",
    "max_tokens": 2048
  }'
```

You may provide a non-empty OpenRouter-compatible `messages` array instead of `prompt`. Roles, content, total serialized size, token count, temperature, and top-p are validated. The response’s `model_used` is the provider-confirmed model.

## Compare models

```bash
curl -X POST http://127.0.0.1:3188/compare \
  -H 'Content-Type: application/json' \
  -d '{
    "models": ["glm", "claude"],
    "prompt": "Review this architecture.",
    "max_tokens": 2048
  }'
```

Two to four distinct non-empty models are supported. Run, compare, route, decide, and benchmark-refresh requests share a bounded paid-request concurrency limit.

## Route a task with Jev

```bash
curl -X POST http://127.0.0.1:3188/route \
  -H 'Content-Type: application/json' \
  -d '{"task": "Add rate limiting to the login API and write its tests."}'
```

Jev (`HELIOS_JEV_MODEL`, default `typesafe/jev-1.13`) chooses one enabled category from `config/benchmark_sources.json`, using each category's `description`. The response carries `category`, `confidence`, `probabilities`, `needs_confirmation`, the Jev `router` usage, and `selection`: the same object as `/benchmarks/select` for that category.

When confidence is below `min_confidence` (default `0.6`, any value from 0 to 1 may be sent), `needs_confirmation` is `true` and `selection` is `null`: ask the user instead of guessing. If the category's evidence is stale or fails its quality gates, `selection` is `null` and `selection_error` explains why.

## Ask Jev typed questions

```bash
curl -X POST http://127.0.0.1:3188/decide \
  -H 'Content-Type: application/json' \
  -d '{
    "state": "<the output to check>",
    "questions": {
      "has_tests": {"type": "noul", "instructions": "Does it include automated tests?"},
      "risk": {"type": "score", "instructions": "How risky is this change?", "criteria": ["low", "medium", "high"]},
      "owner": {"type": "choice", "instructions": "Which team owns it?", "criteria": {"backend": "APIs and data", "frontend": "User interface"}}
    }
  }'
```

- `noul`: the probability that a condition holds; no `criteria`.
- `score`: `criteria` is an array of 2 to 10 levels, lowest first.
- `choice`: `criteria` maps 2 to 255 option keys to descriptions.

Up to 16 questions per call; names use letters, digits, and underscores. The text plus questions are limited to `HELIOS_MAX_DECISION_CHARS` (40000) characters. The response returns Jev's `answers` keyed by question name, with `model_used` and `usage`.

## Refresh the model catalog

```bash
curl -X POST http://127.0.0.1:3188/refresh-models \
  -H 'Content-Type: application/json' \
  -d '{}'
```

## Public benchmark registry

The registry uses cited public web evidence only. Every category has an explicit official/primary-source domain allowlist. It rejects all other domains, blocked aggregators, display-only or estimated tables, and rankings with fewer than three distinct comparable models.

```bash
curl http://127.0.0.1:3188/benchmarks/status
curl 'http://127.0.0.1:3188/benchmarks?category=coding'
curl 'http://127.0.0.1:3188/benchmarks/select?category=Backend'
```

Category names and aliases are normalized case-insensitively. Selection fails when that category’s evidence is stale, insufficient, or has no eligible OpenRouter model.

Manual stale-only refresh:

```bash
curl -X POST http://127.0.0.1:3188/benchmarks/refresh \
  -H 'Content-Type: application/json' \
  -d '{"only_if_stale":true}'
```

The installer schedules the same operation for Monday at 03:00 local time. A stale-only refresh searches only the categories that are missing, expired, or below the quality gates; categories no longer enabled in the config are dropped. Evidence dated more than `max_evidence_age_days` (180) before the refresh is rejected. Each category has its own `valid_until`; a partial refresh cannot make preserved old evidence appear fresh. If every category fails validation, the published registry remains untouched. Only one refresh runs at a time, including the scheduled one.

## Friendly aliases

Built-in aliases include `glm`, `gemini`, `gemini-flash`, `claude`, `deepseek`, and `qwen`. Exact OpenRouter slugs such as `provider/model` are also accepted.

## Project orchestration

The V2 project API persists projects, dependency-safe task plans, executions, reviews, artifacts, usage, and append-only events in SQLite. Mutating requests accept an `idempotency_key`; updates also accept a `version` for optimistic concurrency.

### Projects

- `GET /v2/projects?limit=<1-200>`
- `POST /v2/projects`
- `GET /v2/projects/{project_id}`
- `POST /v2/projects/{project_id}/plan`
- `POST /v2/projects/{project_id}/{start|pause|resume|cancel}`
- `GET /v2/projects/{project_id}/{tasks|artifacts|events|usage}`

Project creation accepts the objective, constraints, acceptance criteria, and optional token, cost, concurrency, and retry limits. Planning accepts a task array with stable dependency references and rejects missing dependencies or cycles.

### Tasks

- `GET /v2/tasks/{task_id}`
- `POST /v2/tasks/{task_id}/run`
- `POST /v2/tasks/{task_id}/verify`
- `POST /v2/tasks/{task_id}/approve`
- `POST /v2/tasks/{task_id}/request-revision`

Only dependency-ready tasks can run. Execution records the provider-confirmed model, usage, latency, errors, and output artifact hash. Verification and approval are explicit state transitions; a revision request returns the task to a runnable state within its retry budget.

The database uses WAL mode, foreign keys, synchronous durability, credential redaction and confined artifact paths. Opening another client never resets active work. Expired execution leases become blocked and require billing reconciliation, rather than issuing a second paid request.

### 2.2 execution contract

Project execution validates the complete request and live model capabilities before consuming an attempt. It reserves conservative input/output token and dollar headroom atomically, checks per-project concurrency, and pins provider maximum prices. Unknown pricing refuses dispatch. Separately billed reasoning needs an explicit adapter and is refused by budgeted text execution. Actual provider overages are recorded and block further work; local limits cannot reverse provider billing.

All paid outbound OpenRouter calls (including Compare children, Jev and refresh) and Manus creation share process-independent capacity slots. `GET /usage` exposes the gateway ledger: known USD and unknown-cost call counts are separate; Manus credits are not converted to USD. V2 calls appear in both the global and project views, so do not add the totals together. Prompts, credentials and response bodies are excluded from the global ledger.

Task `timeout_seconds` reaches an isolated outbound HTTP exchange with an overall deadline covering connection, headers and body. Timeout kills the local transport, but does not prove provider-side cancellation. Ambiguous calls retain budget reservations. Cancellation is terminal even if a late provider response arrives; known late usage is counted once.

`POST /v2/tasks/{task_id}/enqueue` accepts the same body/version/idempotency key as `/run` and returns HTTP 202 with a durable job. Only explicitly queued tasks run in background. Existing projects remain host-driven. Queue jobs can be inspected with `GET /v2/jobs/{job_id}` and `GET /v2/projects/{project_id}/jobs`. Job `succeeded` means execution was recorded, while the task still requires verification. Pause stops new dispatch; cancellation fences late completion. Restart does not reissue an uncertain claimed job.

`GET /v2/executions/{execution_id}` exposes execution metadata, reservation and billing status. To settle an uncertain outcome, `POST /v2/executions/{execution_id}/reconcile` requires an idempotency key and:

```json
{"billing_evidence":{"source":"provider statement","reference":"generation-id","details":"Final usage confirmed"},"cost_usd":0.002,"tokens":420}
```

Alternatively use `confirmed_not_charged: true`; restarting a no-charge attempt also requires explicit `retry_authorized: true`. Reconciliation records final charges by delta and releases the reservation. It never revives a cancelled project. It is a trusted authenticated host assertion, not an independently authenticated billing provider.

The task prompt includes project scope/constraints and checksum-verified predecessor artifacts with bounded excerpts and provenance. All-chat context is never sent implicitly. Scoped global context requires both `include_global_context: true` and `global_context_scope: "project:<project_id>"`, matching the stored scope.

Verification `decision: "pass"` now requires an evidence **object** bound to the current artifact and every acceptance criterion:

```json
{"decision":"pass","evidence":{"artifact_id":"<result-artifact-id>","checksum_sha256":"<sha256>","checks":[{"criterion":"<exact acceptance criterion>","passed":true,"details":"Observed test result"}],"host_check":{"command":"npm test","exit_code":0,"output":"All checks passed"}}}
```

Independent model reviews must refer to a real persisted review artifact from a different confirmed model family. A client-written model label is insufficient. Host check evidence is supplied by the authenticated orchestrator; actor names are audit labels, not separate identities. Human approval still requires actual user approval, and cannot bypass missing verification evidence.

`GET /health` is process liveness and returns component status and `ready`; `GET /ready` returns 503 if required local components are unhealthy. Partial or stale benchmark coverage is reported separately. `GET /benchmarks/select` accepts JSON-encoded `requirements`; `/route` accepts the same object. Selection checks the live model catalog's context, modalities, pricing and supported reasoning efforts, and returns `evaluation_settings`, `execution_parameters` and rejected candidates. Carry the returned execution parameters into the model call; do not attribute another setting's benchmark score to it.

The official ARC-AGI-2 adapter reads structured source data without a paid extraction call. All adapters require dated, comparable numerical evidence; failed refreshes preserve the previous evidence without renewing its expiry. Obsolete LongBench evidence remains blocked. See [backup and restore](BACKUP_RESTORE.md) for complete archive coverage, isolated restore validation and external-destination configuration.

### Executable settings and transport lifetime

Both MCP selection and routing accept `requirements`. The shared run/enqueue
contract accepts `max_tokens`, `temperature`, `top_p` and `reasoning_effort` as
top-level fields. Evaluated settings outside this contract are ineligible even
when a provider supports them. Requested output bounds are returned in
`execution_parameters`; unsupported direct parameters are rejected before dispatch.

The HTTP child owns its deadline and, on Linux, a parent-death signal. POSIX
children inherit the outbound slot lock so gateway death cannot immediately
reissue its capacity while the child remains alive. Linux parent-death behavior
is covered by a real socket/process regression; Windows crash behavior has not
been validated by this release's Linux test run. Pre-dispatch queue failures
can fail or retry safely without leaving an unreconcilable execution reservation.

The global provider ledger records calls observed by 2.2 onward. It cannot reconstruct historical standalone calls. Existing project usage remains intact; do not treat an initially empty new global ledger as proof that prior usage was zero.
