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

Project state is session-scoped. The skill does not call `/v2/projects` and does not claim restart recovery, persistent pause/resume, or durable budgets.

The Mac agent must be available at `http://127.0.0.1:3188`. If `HELIOS_LOCAL_API_KEY` is enabled in the agent, configure the same value in the MCP process without committing it.
