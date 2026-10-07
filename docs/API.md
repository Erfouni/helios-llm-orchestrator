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

Two to four distinct non-empty models are supported. Run, compare, and benchmark-refresh requests share a bounded paid-request concurrency limit.

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

The installer schedules the same operation for Monday at 03:00 local time. Each category has its own `valid_until`; a partial refresh cannot make preserved old evidence appear fresh. If every category fails validation, the published registry remains untouched.

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

The database uses WAL mode, foreign keys, synchronous durability, startup recovery for orphaned executions, credential redaction before persistence, and allowlisted artifact paths. The hosted service takes daily integrity-checked database backups.
