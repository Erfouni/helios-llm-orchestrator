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

The current HTTP API intentionally exposes primitives rather than a fake project backend. ChatGPT/Codex creates and tracks the task graph in its active session, calls benchmark selection and `/run` for each task, reviews outputs, and integrates accepted results. Persistent `/v2/projects` endpoints are not implemented yet.
