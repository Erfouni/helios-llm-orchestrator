# Helios — Multi-LLM Orchestrator

> Turn ChatGPT or Codex into the lead orchestrator of a specialist AI team.

Helios is a local-first **multi-LLM orchestrator, MCP server, and OpenRouter gateway**. It connects ChatGPT, Codex, and other MCP clients to multiple AI models, then helps route each task to the model best suited to it instead of forcing one model to do everything.

## The goal

The goal of Helios is to make GPT the lead of a professional, multi-model AI team.

A complex project contains different kinds of work: research, product design, backend engineering, frontend implementation, reasoning, OCR, vision, generation, and review. GPT first decomposes the project brief into small, testable tasks with explicit dependencies and acceptance criteria. Helios then classifies every task and gives the orchestrator one secure interface for assigning it to a specialized model through OpenRouter. A web-sourced benchmark registry is refreshed weekly so each assignment can follow current public evidence rather than a permanently hardcoded model list.

For example, a project might use:

| Work type | Illustrative specialist |
| --- | --- |
| Web research and R&D | A search-grounded model such as Perplexity |
| Backend engineering | A leading coding model from the Claude family |
| Frontend implementation | GLM 5.2 or the current frontend benchmark leader |
| Math and reasoning | The strongest available reasoning model |
| Independent review | A different high-performing model family |

These model names are examples, not fixed assignments. The registry can change the selected model as public leaderboards change and as models become available on OpenRouter.

## Big-project orchestration workflow

This is the target Helios V2 project workflow. It makes project decomposition and per-task specialist assignment the central path:

```mermaid
flowchart TB
    U["Project brief<br/>goals • constraints • deliverables"] --> O["ChatGPT / Codex<br/>Lead orchestrator"]
    O <-->|"MCP over stdio"| M["Helios MCP server"]
    M <-->|"Loopback HTTP"| A["Local Helios agent<br/>127.0.0.1:3188"]

    A --> P["Decompose the project"]
    P --> T["Atomic task set<br/>R&D • Product • Backend • Frontend<br/>OCR • Vision • Image/Video • Analysis"]
    T --> D["Validated task DAG<br/>dependencies • inputs • acceptance criteria"]
    D --> Q["Ready-task queue"]
    Q --> C["Classify every task<br/>capability • risk • tools"]

    W["Public benchmarks<br/>and leaderboards"] -->|"Weekly web refresh"| B["Versioned benchmark registry"]
    B -->|"Cited ranking by category"| R["Select the strongest available<br/>specialist for each task"]
    C --> R

    R --> G["OpenRouter"]
    G --> E["Parallel specialist execution<br/>confirmed model ID per task"]
    E --> V["Independent verification<br/>criteria • evidence • tool checks"]
    V -->|"Revision required"| Q
    V -->|"Accepted outputs"| I["GPT integrates the task results"]
    I --> F["Final answer • artifacts<br/>provenance • unresolved risks"]

    classDef client fill:#172554,stroke:#60a5fa,color:#ffffff,stroke-width:2px;
    classDef planning fill:#3b0764,stroke:#c084fc,color:#ffffff,stroke-width:2px;
    classDef evidence fill:#064e3b,stroke:#34d399,color:#ffffff,stroke-width:2px;
    classDef execution fill:#4c0519,stroke:#fb7185,color:#ffffff,stroke-width:2px;
    classDef review fill:#78350f,stroke:#fbbf24,color:#ffffff,stroke-width:2px;
    classDef result fill:#164e63,stroke:#22d3ee,color:#ffffff,stroke-width:2px;

    class U,O client;
    class M,A,P,T,D,Q,C,R planning;
    class W,B evidence;
    class G,E execution;
    class V review;
    class I,F result;
```

For a large project, GPT remains the lead orchestrator: it creates the task graph, Helios chooses a task-specific specialist for each ready node, independent tasks run in parallel, and GPT integrates only accepted outputs. Direct and Compare modes remain available for smaller requests.

The control path stays local: the MCP server communicates over stdio and the HTTP agent accepts loopback traffic only. A public domain is unnecessary unless a remote client must connect directly over the internet.

## How project routing works

1. GPT turns the project objective into atomic tasks with explicit inputs, outputs, dependencies, and acceptance criteria.
2. Helios validates the task DAG and places dependency-satisfied tasks in the ready queue.
3. Every ready task is classified by capability, such as web research, coding, frontend, mathematics, OCR, vision, image generation, or video generation.
4. Helios queries the local benchmark registry separately for each task category.
5. The strongest cited public-benchmark model currently available through OpenRouter is assigned to that task; independent tasks can run in parallel.
6. A separate reviewer checks each output against its acceptance criteria and either accepts it or returns it for revision.
7. GPT integrates the accepted task results into the final project answer and artifacts.

The benchmark registry uses **public web evidence only**. Helios does not claim to run private benchmark tests. The macOS installer schedules a refresh every Monday at 03:00 local time and keeps versioned history.

## What works today

- Live OpenRouter model discovery.
- Direct execution by friendly alias or exact OpenRouter model ID.
- Parallel comparison of two to four models.
- Weekly, citation-backed public benchmark discovery.
- Benchmark-based model selection by task category.
- MCP tools for discovery, execution, comparison, registry lookup, and refresh.
- Loopback-only local service with credentials stored outside Git.
- macOS LaunchAgent installation for the service and weekly refresh.

## Helios V2 direction

The V2 goal is to extend the current router into a durable big-project orchestrator that can decompose a brief into a task graph, assign specialists, run independent work in parallel, verify outputs with a separate reviewer, preserve project state, and integrate accepted results.

That orchestration layer is documented as an implementation roadmap; it is not presented as a completed feature in the current release. See [Helios V2 Technical Specification](docs/HELIOS_V2_TECHNICAL_SPEC.md).

## Quick start on macOS

Requirements: macOS, Python 3.10+, Node.js 22+, npm, and an OpenRouter API key.

```bash
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
cd helios-llm-orchestrator
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

## MCP tools

- `openrouter_list_models`
- `openrouter_run_model`
- `openrouter_compare_models`
- `helios_get_benchmark_registry`
- `helios_select_benchmark_model`
- `helios_refresh_benchmarks`

## HTTP API

- `GET /health`
- `GET /models?search=<query>&limit=<1-200>`
- `POST /run`
- `POST /compare`
- `POST /refresh-models`
- `GET /benchmarks/status`
- `GET /benchmarks?category=<category>`
- `GET /benchmarks/select?category=<category>`
- `POST /benchmarks/refresh`

See [API documentation](docs/API.md) for request examples and [راهنمای فارسی](docs/USAGE_FA.md) for the Persian guide.

## Security

- No credential is committed.
- `.env`, logs, build output, and dependencies are ignored.
- The default listener is loopback only; non-loopback configuration is rejected.
- OpenRouter credentials are fetched from macOS Keychain or a process environment variable.
- External model output is treated as untrusted input.
- Model routing never grants an external model direct authority to write files or publish content.

Run the checks before every commit:

```bash
npm ci
npm test
npm run scan:secrets
```

See [Security Policy](SECURITY.md).

## License

MIT
