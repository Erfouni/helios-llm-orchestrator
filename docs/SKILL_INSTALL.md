# Installing the Helios skill

The reusable skill is in `skill/helios`.

## Codex installation

```bash
mkdir -p ~/.codex/skills
cp -R skill/helios ~/.codex/skills/helios
```

Restart or refresh the client so it discovers the skill. The skill contains no credentials.

## What it enables

- **Direct:** route an explicitly named external model through `/run`.
- **Compare:** run two to four models through `/compare`.
- **Benchmark routing:** inspect cited evidence and select an eligible specialist for a task category.
- **Project orchestration:** decompose a complex brief in ChatGPT/Codex, show the reviewed plan, obtain approval, run up to four ready tasks concurrently, independently review material outputs, and integrate accepted results.
- **Local social agents:** operate separately installed LinkedIn and Instagram agents under their own authorization and confirmation rules.

Project state is durable. The skill uses the native V2 project tools for restart recovery, dependency tracking, pause/resume/cancel, budgets, task review, artifacts, usage, and event history.

For the hosted deployment, use the authenticated GPT Computer MCP endpoint. Helios itself remains bound to `http://127.0.0.1:3188` on the server and is not exposed directly. Local installations may use the loopback endpoint. If `HELIOS_LOCAL_API_KEY` is enabled, configure the same value in the MCP process without committing it.

The host’s existing cloud LinkedIn connector remains separate. Do not export browser cookies or LinkedIn credentials into Helios; external OpenRouter models do not receive connector authority.
