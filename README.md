# Helios Multi-Model Router

Helios is a local, loopback-only gateway that lets ChatGPT, Codex, or any MCP client route a task to models available through OpenRouter. It supports direct model calls, live model discovery, and side-by-side comparison.

The repository also includes the `helios` skill. That skill can route model requests through this gateway and, when the separate local LinkedIn Agent is installed, operate the owner's authorized LinkedIn endpoints on port 3190.

## Architecture

```text
ChatGPT / Codex / MCP client
            |
       MCP over stdio
            |
  http://127.0.0.1:3188
            |
        OpenRouter
            |
 GLM / Gemini / Claude / DeepSeek / Qwen / ...
```

No purchased domain is needed for local use. A public HTTPS address is required only when a remote client must connect directly to the MCP server over the internet. The local Mac/MCP setup uses loopback and stdio instead.

## Capabilities

- Search the live OpenRouter model catalog.
- Run one requested model by alias or exact OpenRouter slug.
- Compare two to four models in parallel.
- Refresh a web-only public benchmark registry every week and select the highest cited ranked model available on OpenRouter for each task category.
- Report the model actually returned by OpenRouter.
- Keep the OpenRouter credential outside Git; on macOS it is stored in Keychain.
- Bind the HTTP agent to loopback only.
- Optionally protect local requests with an additional bearer token.
- Install as a macOS LaunchAgent.
- Install the included Helios skill for model routing and optional LinkedIn Agent operations.

## Quick start on macOS

Requirements: macOS, Python 3.10+, Node.js 22+, npm, and an OpenRouter API key.

```bash
git clone git@github.com:mesutfd/helios-multimodel-router.git
cd helios-multimodel-router
./scripts/install-macos.sh
```

The installer asks for the OpenRouter key with hidden input and stores it in macOS Keychain. It does not write the key to this repository.

Verify the HTTP agent:

```bash
curl http://127.0.0.1:3188/health
```

Start the MCP server manually:

```bash
npm run mcp:start
```

For MCP client configuration, copy [mcp/client-config.example.json](mcp/client-config.example.json), replace the absolute project path, and add the entry to your MCP client.

## API

- `GET /health`
- `GET /models?search=<query>&limit=<1-200>`
- `POST /run`
- `POST /compare`
- `POST /refresh-models`
- `GET /benchmarks/status`
- `GET /benchmarks?category=<category>`
- `GET /benchmarks/select?category=<category>`
- `POST /benchmarks/refresh`

See [docs/API.md](docs/API.md) for request examples and [docs/USAGE_FA.md](docs/USAGE_FA.md) for the Persian guide. The macOS installer also installs a Monday 03:00 local-time benchmark refresh job. It performs public web search only and does not run private model evaluations.

## Security

- No credential is committed.
- `.env`, logs, build output, and dependencies are ignored.
- The default listener is loopback only; non-loopback configuration is rejected.
- OpenRouter credentials are fetched from macOS Keychain or a process environment variable.
- Public writes to LinkedIn are not implemented in this model router; the included skill delegates those only to the separate local LinkedIn Agent and requires explicit confirmation.

Run the checks before every commit:

```bash
npm ci
npm test
npm run scan:secrets
```

See [SECURITY.md](SECURITY.md).

## License

MIT
