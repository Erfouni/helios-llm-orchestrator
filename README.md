# Helios — Multi-LLM Orchestrator

> Turn ChatGPT or Codex into the lead orchestrator of a specialist AI team.

Helios is a **multi-LLM orchestrator, MCP server, OpenRouter gateway, and durable project-memory service**. It lets ChatGPT, Codex, or another MCP client break a complex project into focused tasks, select an evidence-backed specialist for each task, review the results, and integrate the accepted work.

## Project goal

One model should not have to be the best at everything. Helios makes GPT the lead of a professional multi-model team:

- a research specialist can handle R&D;
- a coding specialist can handle backend engineering;
- a frontend specialist can implement the interface;
- vision, OCR, math, or generation specialists can handle their own task types;
- a different model family can independently review important outputs.

The examples are intentionally not hardcoded. Helios refreshes a cited public benchmark registry weekly and chooses the highest-ranked eligible model currently available through OpenRouter for each category.

## How a big project runs today

The workflow below is current behavior when the Helios skill is used by ChatGPT or Codex. Project state is persisted in SQLite on the Helios host, survives service restarts, and can be resumed from another client conversation.

```mermaid
flowchart TB
    U["Project brief<br/>goals • constraints • deliverables"] --> O["ChatGPT / Codex<br/>Lead orchestrator"]
    O --> P["Decompose into atomic tasks"]
    P --> D["Reviewed task DAG<br/>dependencies • inputs • acceptance"]
    D --> Q["Ready-task queue<br/>up to 4 independent tasks"]

    Q --> C["Classify each task<br/>research • coding • frontend<br/>reasoning • vision • generation"]
    C --> M["Helios MCP server<br/>project memory + specialist execution"]
    M <-->|"Authenticated remote MCP"| A["GPT Computer Helios service<br/>loopback-only 127.0.0.1:3188"]

    W["Official public benchmarks<br/>leaderboards • primary papers"] -->|"Weekly web refresh"| B["Versioned registry<br/>citations • per-category freshness"]
    B --> R["Highest-ranked eligible<br/>OpenRouter specialist"]
    A --> R

    R --> G["OpenRouter"]
    G --> E["Parallel specialist outputs<br/>confirmed model ID + usage"]
    E --> V{"Acceptance and<br/>independent review"}
    V -->|"Revise once"| Q
    V -->|"Accepted"| I["GPT integrates results"]
    I --> F["Final deliverable<br/>provenance • risks • artifacts"]

    classDef client fill:#172554,stroke:#60a5fa,color:#ffffff,stroke-width:2px;
    classDef planning fill:#3b0764,stroke:#c084fc,color:#ffffff,stroke-width:2px;
    classDef evidence fill:#064e3b,stroke:#34d399,color:#ffffff,stroke-width:2px;
    classDef execution fill:#4c0519,stroke:#fb7185,color:#ffffff,stroke-width:2px;
    classDef review fill:#78350f,stroke:#fbbf24,color:#ffffff,stroke-width:2px;
    classDef result fill:#164e63,stroke:#22d3ee,color:#ffffff,stroke-width:2px;

    class U,O client;
    class P,D,Q,C,M,A planning;
    class W,B,R evidence;
    class G,E execution;
    class V review;
    class I,F result;
```

### Orchestration sequence

1. GPT defines the objective, requirements, constraints, and observable acceptance criteria.
2. It decomposes the project into at most 12 atomic tasks and validates an acyclic dependency graph.
3. It classifies every model task and asks Helios for the matching benchmark-guided specialist.
4. It shows the reviewed execution plan before paid model calls.
5. After approval, it runs dependency-ready tasks in parallel batches of up to four.
6. Material outputs are checked against their acceptance criteria and, where practical, reviewed by a different model family.
7. GPT integrates only accepted outputs and reports model provenance, usage, unresolved risks, and blocked items.

Host-tool tasks—such as browsing, files, terminal commands, GitHub, or media generation—use the real tools available to ChatGPT/Codex. External models do not receive independent tool authority.

## Current capability boundary

Available now:

- ChatGPT/Codex-led project decomposition with durable task graphs;
- per-task benchmark-guided specialist selection;
- parallel model execution, independent review, and final integration;
- Direct mode for one named model and Compare mode for two to four models;
- live OpenRouter model discovery;
- weekly, citation-backed public benchmark discovery with per-category freshness;
- restart-safe SQLite/WAL project memory and a persistent dependency scheduler;
- start, pause, resume, cancel, verification, approval, and revision workflows;
- enforced token, cost, concurrency, and retry controls;
- append-only event, execution, usage, and artifact histories;
- loopback-only HTTP with MCP-over-stdio or authenticated remote MCP access;
- native startup automation, daily database backups, CloudWatch logging, and Monday 03:00 benchmark refresh.

The implemented lifecycle follows [Helios V2](docs/HELIOS_V2_TECHNICAL_SPEC.md) while retaining the original Direct, Compare, and benchmark-routing APIs.

## Benchmark routing

Helios performs **web search only** for registry updates; it does not run private benchmark tests. Evidence must:

- come from a per-category allowlist of official leaderboards, benchmark sites, or primary papers;
- provide one comparable ranking with at least three distinct models;
- include citation URLs returned by web search;
- avoid display-only tables, aggregators, estimates, and fabricated/composite scores.

If evidence is stale, insufficient, or the ranked models are unavailable on OpenRouter, Helios reports the limitation instead of claiming a strongest model.

## Installation

Requirements on both platforms: Python 3.10+, Node.js 22+, npm, Git, and an OpenRouter API key.

### macOS

```bash
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
cd helios-llm-orchestrator
./scripts/install-macos.sh
```

The installer validates runtime versions, runs tests and security checks, stores the API key in macOS Keychain, installs both LaunchAgents, and attempts an initial stale-only benchmark refresh. The service uses legacy LaunchAgent and Keychain labels for upgrade compatibility.

### Windows 10/11

Open PowerShell as the Windows user who will run Helios:

```powershell
git clone https://github.com/Erfouni/helios-llm-orchestrator.git
Set-Location helios-llm-orchestrator
powershell -ExecutionPolicy Bypass -File .\scripts\install-windows.ps1
```

The Windows installer performs the same checks, encrypts the OpenRouter key with Windows DPAPI for the current user, creates an at-logon Scheduled Task for the local agent, creates the Monday 03:00 benchmark-refresh task, and attempts the initial refresh. See the [Windows guide](docs/WINDOWS.md) and [Windows MCP example](mcp/client-config.windows.example.json) for configuration, key rotation, task management, and uninstall steps.

Verify the local agent:

```bash
curl http://127.0.0.1:3188/health
```

Start the MCP server manually:

```bash
npm run mcp:start
```

Copy [mcp/client-config.example.json](mcp/client-config.example.json), replace the absolute project path, and add it to your MCP client configuration.

## MCP tools

| Tool | Purpose |
| --- | --- |
| `openrouter_list_models` | Search the live OpenRouter catalog |
| `openrouter_run_model` | Run one approved specialist task |
| `openrouter_compare_models` | Compare two to four models |
| `helios_get_benchmark_registry` | Inspect evidence and freshness |
| `helios_select_benchmark_model` | Select an eligible specialist by category |
| `helios_refresh_benchmarks` | Explicitly run a stale-only or full web refresh |
| `helios_create_project` | Create a durable project with budgets and controls |
| `helios_plan_project` | Persist and validate a dependency-safe task plan |
| `helios_project_action` | Start, pause, resume, or cancel a project |
| `helios_get_project_memory` | Retrieve project state, tasks, usage, artifacts, and events |
| `helios_run_task` | Execute one ready task through OpenRouter |
| `helios_review_task` | Verify, approve, or request revision of a task |

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
- `GET|POST /v2/projects`
- `GET /v2/projects/{project_id}`
- `POST /v2/projects/{project_id}/{plan|start|pause|resume|cancel}`
- `GET /v2/projects/{project_id}/{tasks|artifacts|events|usage}`
- `GET /v2/tasks/{task_id}`
- `POST /v2/tasks/{task_id}/{run|verify|approve|request-revision}`

See [API documentation](docs/API.md), the [V2 technical specification](docs/HELIOS_V2_TECHNICAL_SPEC.md), and the [راهنمای فارسی](docs/USAGE_FA.md).

## Security

- The listener rejects non-loopback hosts.
- OpenRouter credentials can come from AWS Secrets Manager, macOS Keychain, a Windows DPAPI-encrypted user credential, or the process environment and are never returned or persisted in project memory.
- Optional `HELIOS_LOCAL_API_KEY` authentication can protect local HTTP calls when the MCP process is configured with the same value.
- Request bodies, messages, numeric parameters, paid concurrency, and model counts are bounded.
- HTTP errors do not expose unexpected internal exception details.
- External model output is treated as untrusted data.
- Routing never grants an external model authority to write files or publish content.
- The existing cloud LinkedIn connector remains a separate host capability; LinkedIn cookies and credentials are not copied into Helios or exposed to external models.

Run the complete verification suite:

```bash
npm ci
npm test
npm run audit:high
npm run scan:secrets
npm run audit:prod
```

The dependency audit fails CI for high- and critical-severity findings. Moderate findings are still printed for review instead of being hidden.

See [Security Policy](SECURITY.md).

## License

MIT
