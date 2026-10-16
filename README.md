# Helios

Helios lets ChatGPT or Codex run a project with a team of AI models. It splits the work into tasks, picks the best-ranked model for each one from public benchmarks, runs them through OpenRouter, checks the results, and keeps the whole project in durable memory.

## How it works

1. You describe the project, and ChatGPT or Codex breaks it into tasks.
2. [Jev](https://openrouter.ai/typesafe/jev-1.13), a fast decision model, sorts each task into a category such as coding, research, or vision.
3. Helios picks the top model for that category from a weekly registry of cited public benchmarks.
4. Tasks run through OpenRouter, or Manus for long agent jobs, and each output is checked before it is accepted.
5. Plans, results, costs, and history are stored in SQLite, so a project survives restarts and can continue in another chat.

## Quick start

You need Python 3.10+, Node.js 22+, and an [OpenRouter API key](https://openrouter.ai/keys).

```bash
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
cd helios-llm-orchestrator
npm ci && npm test
```

Start Helios with your key (on Windows, see the [Windows guide](docs/WINDOWS.md)):

```bash
read -rs OPENROUTER_API_KEY && export OPENROUTER_API_KEY
python3 agent/server.py
```

Check it from another terminal:

```bash
curl http://127.0.0.1:3188/health
```

Then add [mcp/client-config.example.json](mcp/client-config.example.json) to your MCP client, with the path to your clone.

The benchmark registry starts empty. The first refresh makes one paid web search per category:

```bash
curl -X POST http://127.0.0.1:3188/benchmarks/refresh -H "Content-Type: application/json" -d '{"only_if_stale": true}'
```

## Install as a service

- **macOS:** `./scripts/install-macos.sh` keeps the key in the Keychain.
- **Windows:** `scripts\install-windows.ps1` encrypts the key with DPAPI. See the [Windows guide](docs/WINDOWS.md).
- **Linux server:** use the systemd units in [deploy/](deploy).

The macOS and Windows installers run the tests, start Helios at login, and refresh the benchmarks every Monday.

## Tools

| Area | MCP tools |
| --- | --- |
| Run models | `openrouter_run_model`, `openrouter_compare_models`, `openrouter_list_models` |
| Pick a model | `helios_route_task`, `helios_select_benchmark_model`, `helios_decide` |
| Projects | `helios_create_project`, `helios_plan_project`, `helios_project_action`, `helios_run_task`, `helios_review_task`, `helios_get_project_memory` |
| Manus | `manus_create_task`, `manus_get_task`, `manus_list_task_messages`, `manus_stop_task` |
| Context and benchmarks | `helios_get_global_context`, `helios_update_global_context`, `helios_get_benchmark_registry`, `helios_refresh_benchmarks` |

## Security

Helios listens only on `127.0.0.1`, never returns API keys, and treats model output as untrusted data. See [SECURITY.md](SECURITY.md).

## Documentation

- [HTTP API](docs/API.md)
- [راهنمای فارسی](docs/USAGE_FA.md)
- [Windows guide](docs/WINDOWS.md)
- [V2 design](docs/HELIOS_V2_TECHNICAL_SPEC.md)

## License

MIT
