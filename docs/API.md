# HTTP API

Base URL: `http://127.0.0.1:3188`

All responses are JSON. If `HELIOS_LOCAL_API_KEY` is configured, send:

```http
Authorization: Bearer <local-secret>
```

The `/health` endpoint remains available without authentication and never exposes a credential.

## Health

```bash
curl http://127.0.0.1:3188/health
```

## Search models

```bash
curl 'http://127.0.0.1:3188/models?search=gemini&limit=10'
```

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

The response includes `model_requested`, `model_resolved`, and `model_used`. Treat `model_used` as the confirmed provider response.

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

Two to four models are supported per request.

## Refresh catalog

```bash
curl -X POST http://127.0.0.1:3188/refresh-models \
  -H 'Content-Type: application/json' \
  -d '{}'
```

## Weekly public benchmark registry

The registry is built only from cited public web-search benchmark or leaderboard results. It does not run candidate models through internal tests. The current and historical registries are stored under the user's Helios application state directory, not in Git.

Read freshness:

```bash
curl http://127.0.0.1:3188/benchmarks/status
```

Read all categories or filter one:

```bash
curl 'http://127.0.0.1:3188/benchmarks?category=coding'
```

Select the highest cited public-benchmark model currently available on OpenRouter:

```bash
curl 'http://127.0.0.1:3188/benchmarks/select?category=backend'
```

Run a refresh manually:

```bash
curl -X POST http://127.0.0.1:3188/benchmarks/refresh \\
  -H 'Content-Type: application/json' \\
  -d '{"only_if_stale":true}'
```

The macOS installer schedules this refresh for Monday at 03:00 local time. If every category fails validation, the previous registry remains untouched. Partial refreshes preserve earlier values for failed categories.

## Friendly aliases

Built-in aliases include `glm`, `gemini`, `gemini-flash`, `claude`, `deepseek`, and `qwen`. Dynamic aliases select a recent matching model from the live catalog unless an exact default is configured in `.env`. An exact OpenRouter slug such as `provider/model` can always be supplied.
