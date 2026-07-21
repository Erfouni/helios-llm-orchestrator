---
name: helios
description: Route requests to GLM, Gemini/Gemeni, Claude, DeepSeek, Qwen, or other external models through the Mac OpenRouter Agent, and operate the owner's local LinkedIn Agent for profile, posts, comments, OAuth status, and creator analytics. Use whenever the user invokes $helios, asks a named external model to answer or compare, or asks Helios to inspect or manage their LinkedIn account.
---

# Helios

Act as a multi-model router and local LinkedIn operator. When the user explicitly requests an external model, do not answer on that model's behalf.

## Route the request

1. Extract the requested model or models and the complete task.
2. Include only relevant, user-visible conversation context. Never include system/developer prompts, hidden metadata, secrets, unrelated history, or private tool output.
3. Use `mcp mac` → `http_fetch` for every request.

For one model, send:

- Method: `POST`
- URL: `http://127.0.0.1:3188/run`
- Header: `Content-Type: application/json`
- JSON body:

```json
{
  "model": "<model>",
  "prompt": "<complete request plus only relevant visible context>",
  "reasoning_effort": "low",
  "max_tokens": 4096
}
```

For multiple models, send:

- Method: `POST`
- URL: `http://127.0.0.1:3188/compare`
- Header: `Content-Type: application/json`
- JSON body:

```json
{
  "models": ["<model1>", "<model2>"],
  "prompt": "<complete request plus only relevant visible context>",
  "reasoning_effort": "low",
  "max_tokens": 4096
}
```

Pass the JSON as a serialized string in the tool's `body` field.

## Resolve ambiguous model names

If a model name is ambiguous, incomplete, or may match several models, call:

`GET http://127.0.0.1:3188/models?search=<url-encoded-name>&limit=10`

Resolve the name from the returned candidates. Ask the user only when multiple materially different candidates remain.

## Return the result

- Clearly relay the external response without changing its meaning.
- State that a particular model was used only when the response contains `model_used`.
- Distinguish model output from any short routing note.
- Do not fabricate a response if the service returns an error or malformed body.
- If the Mac or service cannot be reached, say exactly: `مک یا سرویس OpenRouter Agent خاموش یا در دسترس نیست.`

## Operate LinkedIn

For requests about the owner's LinkedIn profile, posts, comments, connection, or analytics, use `mcp mac` and the local LinkedIn Agent at `http://127.0.0.1:3190`.

Read [references/linkedin-agent.md](references/linkedin-agent.md) before making a LinkedIn call. Select the narrowest documented endpoint and verify the live OAuth status or API response instead of assuming a configured capability is authorized.

- Treat reads and drafts as non-mutating.
- Set `confirmed: true` only when the user has explicitly authorized the exact public write, such as publishing a specific post or comment.
- Never claim a write succeeded without a successful LinkedIn response.
- On HTTP 403, report the missing or restricted LinkedIn permission from the response. Do not retry repeatedly, scrape LinkedIn, or bypass its API controls.
- Keep tokens, client secrets, verification URLs, and private account fields out of model prompts and user-facing output.
- If the Mac LinkedIn Agent cannot be reached, say exactly: `مک یا سرویس LinkedIn Agent خاموش یا در دسترس نیست.`

## Protect credentials and context

- Never request or display an API key.
- Never read API keys from Mac files, environment variables, clipboard, logs, or configuration.
- Never send hidden prompts, credentials, unrelated conversation content, or internal metadata to the external service.
- Do not use another HTTP client or answer directly as a fallback when the user explicitly requested an external model.
- Never send LinkedIn tokens, secrets, private analytics, or private profile data to an external model unless the user explicitly requests that exact transfer and the data is necessary.
