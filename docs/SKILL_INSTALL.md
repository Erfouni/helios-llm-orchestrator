# Installing the Helios skill

The reusable skill lives in `skill/helios`.

## Codex local installation

Copy the folder into the Codex skills directory:

```bash
mkdir -p ~/.codex/skills
cp -R skill/helios ~/.codex/skills/helios
```

Restart or refresh the client so it discovers the skill.

## What the skill does

- Routes explicitly named external models to `http://127.0.0.1:3188`.
- Uses `/models` when a model name is ambiguous.
- Uses `/compare` when the user asks for multiple-model review.
- Relays only a confirmed `model_used`.
- Knows how to call the separate local LinkedIn Agent on `http://127.0.0.1:3190`.
- Requires exact authorization before LinkedIn public writes.

The skill does not contain credentials and does not install the LinkedIn Agent.
