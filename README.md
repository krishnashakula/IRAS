<div align="center">

<h1>⚡ IRAS</h1>
<h3>Intelligent Incident Response Agent System</h3>

<p><em>Autonomous AI agents that triage, investigate, remediate, and document production incidents — with a human in the loop.</em></p>

<br/>

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-FF6B35?style=flat-square)](https://langchain-ai.github.io/langgraph/)
[![Pydantic AI](https://img.shields.io/badge/Pydantic_AI-latest-E92063?style=flat-square)](https://ai.pydantic.dev)
[![Claude](https://img.shields.io/badge/Claude-Sonnet_%7C_Haiku-blueviolet?style=flat-square&logo=anthropic&logoColor=white)](https://anthropic.com)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen?style=flat-square)](https://coverage.readthedocs.io)
[![Tests](https://img.shields.io/badge/tests-292%20passing-brightgreen?style=flat-square)](https://pytest.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

<br/>

[**Quick Start**](#-quick-start) · [**How It Works**](#-how-it-works) · [**Architecture**](#-architecture) · [**API Reference**](#-api-reference) · [**Configuration**](#-configuration) · [**Contributing**](#-contributing)

</div>

---

## What Is IRAS?

When a production alert fires at 3 AM, IRAS handles the full first-response lifecycle automatically:

1. **Ingests** the alert from any monitoring system (Prometheus AlertManager, PagerDuty, Datadog, etc.)
2. **Triages** severity (P0–P3) and identifies affected services using Claude Haiku
3. **Gathers context** — logs from Elasticsearch/Loki, metrics from Prometheus, recent deployments from GitHub
4. **Runs root-cause analysis** with Claude Sonnet, retrying with broader context if confidence is too low
5. **Generates a step-by-step remediation plan** with rollback commands for every step
6. **Pauses for human approval** via a Slack message with Approve/Reject buttons
7. **Applies the fix** if approved, or **pages on-call** via PagerDuty if rejected or confidence exhausted
8. **Writes a structured post-mortem** — timeline, root cause, resolution, action items — stored in PostgreSQL and posted to Slack

Every agent output is **type-safe** — no raw strings, only validated Pydantic models.  
Every graph run is **persisted in PostgreSQL** so it survives restarts and can be resumed across processes.

---

## Demo

```
Alert: "High error rate on payment-service — http_error_rate: 45% (threshold: 5%)"

[10:30:01] IRAS      ▶ Ingested incident abc12345
[10:30:02] Triage    ▶ P1 | payment-service | ~5,000 users affected | confidence: 0.9
[10:30:04] Context   ▶ DB connection errors in logs, deployment 2m before alert
[10:30:07] RCA       ▶ DB connection pool exhausted after canary deploy | confidence: 0.88 ✓
[10:30:09] Plan      ▶ 3-step remediation | low risk | rollback commands ready
[10:30:09] Slack     ▶ Sent approval request to #incidents  [Approve] [Reject]

  ... on-call engineer reviews and clicks Approve (1m 35s later) ...

[10:31:44] Applying  ▶ Step 1/3 — increase DB_POOL_SIZE from 10 to 50
[10:31:45] Applying  ▶ Step 2/3 — rolling restart payment-service pods
[10:31:45] Applying  ▶ Step 3/3 — verify error rate dropped below 2%
[10:31:46] PostMort. ▶ Written and posted to #incidents
[10:31:46] Resolved  ▶ Total response time: 1m 45s
```

---

## Architecture

### System Overview

```mermaid
graph TB
    subgraph Sources["Alert Sources"]
        PD[PagerDuty]
        PROM[Prometheus AlertManager]
        DD[Datadog / Grafana]
        ANY[Any JSON Webhook]
    end

    subgraph API["FastAPI REST API"]
        WH["POST /webhook/alert"]
        APR["POST /incidents/{id}/approve"]
        REJ["POST /incidents/{id}/reject"]
        HLT["GET /health"]
    end

    subgraph Graph["LangGraph State Machine"]
        ING[Ingestion]
        TRI["Triage Agent\nClaude Haiku"]
        CTX["Context Gathering\nClaude Haiku"]
        RCA["RCA Agent\nClaude Sonnet"]
        GEN["Generate Plan\nClaude Sonnet"]
        APP["Approval\n⏸ interrupt"]
        REM[Apply Remediation]
        ESC[Escalation]
        PM["Post-mortem Agent\nClaude Sonnet"]
    end

    subgraph Integrations["External Integrations"]
        SL[Slack]
        PG2[PagerDuty]
        LOGS["Elasticsearch / Loki"]
        METRICS[Prometheus]
        DEPLOY[GitHub Deployments]
        DB[(PostgreSQL)]
    end

    Sources --> WH
    WH --> ING
    ING --> TRI --> CTX --> RCA
    RCA -->|"conf < 0.7, attempts < max"| CTX
    RCA -->|"conf >= 0.7"| GEN
    RCA -->|"attempts exhausted"| ESC
    GEN --> APP
    APP -->|approved| REM
    APP -->|rejected| ESC
    REM --> PM
    ESC --> PM
    PM --> DB
    PM --> SL
    APR --> APP
    REJ --> APP
    CTX --> LOGS & METRICS & DEPLOY
    ESC --> PG2 & SL
    GEN --> SL
    Graph --> DB
```

### Graph Execution Flow

```mermaid
flowchart LR
    START(( START )) --> ING

    ING["`**ingestion**
    Validate payload
    Stamp UUID + time
    Init state`"]

    ING --> TRI["`**triage**
    Claude Haiku
    P0–P3 severity
    Affected services`"]

    TRI --> CTX["`**context_gathering**
    Claude Haiku
    Fetch logs
    Fetch metrics
    Fetch deployments`"]

    CTX --> RCA["`**rca**
    Claude Sonnet
    Root cause
    Confidence score`"]

    RCA -->|"conf >= 0.7"| GEN["`**generate_plan**
    Claude Sonnet
    Remediation steps
    Rollback commands
    Notify Slack`"]

    RCA -->|"conf < 0.7
    attempts < 3"| CTX

    RCA -->|"attempts = 3"| ESC

    GEN --> APP["`**approval** ⏸
    LangGraph interrupt
    Awaits human via
    POST /approve`"]

    APP -->|"approved=True"| REM["`**apply_remediation**
    Execute each step
    Rollback on failure`"]

    APP -->|"approved=False"| ESC["`**escalation**
    PagerDuty trigger
    Slack alert`"]

    REM --> PM["`**postmortem**
    Claude Sonnet
    Timeline + RCA
    Action items
    Persist to DB`"]

    ESC --> PM
    PM --> END(( END ))

    style APP fill:#ff9800,color:#000
    style ESC fill:#f44336,color:#fff
    style REM fill:#4caf50,color:#fff
    style PM fill:#2196f3,color:#fff
```

### Request Lifecycle

```mermaid
sequenceDiagram
    actor Monitor as Monitoring System
    participant API as FastAPI
    participant Graph as LangGraph
    participant Claude as Claude (Anthropic)
    participant Tools as External Tools
    participant DB as PostgreSQL
    participant Slack as Slack
    actor Human as On-Call Engineer

    Monitor->>API: POST /webhook/alert
    API-->>Monitor: 202 {"incident_id": "abc123"}
    API->>Graph: ainvoke(state, thread_id="abc123")

    Graph->>Graph: ingestion — validate + init state
    Graph->>Claude: triage_agent
    Claude-->>Graph: TriageResult {severity: P1, confidence: 0.9}

    Graph->>Claude: context_agent (tool-calling)
    Claude->>Tools: fetch_logs() + fetch_metrics() + fetch_deployments()
    Tools-->>Claude: raw evidence
    Claude-->>Graph: ContextBundle {logs, metrics, deployments}

    Graph->>Claude: rca_agent
    Claude-->>Graph: RootCauseHypothesis {confidence: 0.88} ✓

    Graph->>Claude: remediation_agent
    Claude-->>Graph: RemediationPlan {3 steps + rollbacks}

    Graph->>Slack: Post approval with [Approve] [Reject]
    Note over Graph,DB: interrupt() — graph pauses, state checkpointed to PostgreSQL

    Human->>API: POST /incidents/abc123/approve
    API->>Graph: Command(resume={"approved": True})

    Graph->>Graph: apply_remediation — execute all steps
    Graph->>Claude: postmortem_agent
    Claude-->>Graph: PostMortem {timeline, root_cause, action_items}

    Graph->>DB: INSERT INTO postmortems
    Graph->>Slack: Post post-mortem summary
```

### Project Structure

```
IRAS/
├── src/iras/
│   ├── api/
│   │   ├── app.py                    # FastAPI lifespan: init checkpointer → build graph
│   │   ├── background.py             # Approval timeout monitor
│   │   └── routes/
│   │       ├── webhook.py            # POST /webhook/alert
│   │       └── approval.py           # POST /incidents/{id}/approve|reject
│   │
│   ├── graph/
│   │   ├── builder.py                # Wire 9 nodes + conditional edges → compile
│   │   ├── checkpointer.py           # AsyncPostgresSaver (singleton + asyncio.Lock)
│   │   ├── state.py                  # IncidentState TypedDict
│   │   └── nodes/
│   │       ├── ingestion.py          # Validate payload, stamp UUID + timestamp
│   │       ├── triage.py             # → triage_agent (Claude Haiku)
│   │       ├── context_gathering.py  # → context_agent + tool calls
│   │       ├── rca.py                # → rca_agent + retry routing
│   │       ├── generate_plan.py      # → remediation_agent + Slack notify
│   │       ├── approval.py           # interrupt() human-in-the-loop checkpoint
│   │       ├── apply_remediation.py  # Execute steps + rollback on failure
│   │       ├── escalation.py         # PagerDuty trigger + Slack escalation
│   │       └── postmortem.py         # → postmortem_agent + persist to DB
│   │
│   ├── agents/
│   │   ├── triage.py                 # Claude Haiku — fast severity classification
│   │   ├── context_gathering.py      # Claude Haiku — tool-calling context agent
│   │   ├── rca.py                    # Claude Sonnet — deep root cause analysis
│   │   ├── remediation.py            # Claude Sonnet — step-by-step plan generation
│   │   ├── postmortem.py             # Claude Sonnet — structured post-mortem
│   │   └── deps.py                   # Dependency injection dataclasses
│   │
│   ├── models/
│   │   └── incident.py               # TriageResult · ContextBundle · RootCauseHypothesis
│   │                                 # RemediationPlan · RemediationStep · PostMortem
│   │
│   ├── tools/
│   │   ├── log_fetcher.py            # Elasticsearch + Loki HTTP clients
│   │   ├── metrics.py                # Prometheus HTTP client
│   │   ├── deployment.py             # GitHub Deployments API client
│   │   ├── slack.py                  # Slack SDK wrapper + MockSlackClient
│   │   └── pagerduty.py              # PagerDuty Events API v2 + MockPagerDutyClient
│   │
│   └── config/
│       └── settings.py               # Pydantic Settings — reads .env
│
├── tests/
│   ├── unit/                         # Fully mocked, no external calls
│   ├── integration/                  # Live service tests (opt-in markers)
│   ├── e2e/                          # Full graph runs with MemorySaver
│   └── stress/                       # 47 adversarial + real-world scenarios
│
├── .env.example                      # Environment variable template
├── pyproject.toml                    # Project metadata + dependencies
└── run.py                            # Development server launcher
```

---

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Docker (for PostgreSQL)
- An [Anthropic API key](https://console.anthropic.com/)

### 1 — Clone and install

```bash
git clone https://github.com/your-org/iras.git
cd iras

python -m venv .venv
source .venv/bin/activate        # Linux/macOS
# .\.venv\Scripts\Activate.ps1  # Windows PowerShell

pip install -e ".[dev]"
```

### 2 — Start PostgreSQL

```bash
docker run -d --name iras-postgres \
  -e POSTGRES_USER=iras \
  -e POSTGRES_PASSWORD=secret \
  -e POSTGRES_DB=iras \
  -p 5432:5432 \
  postgres:16
```

### 3 — Configure environment

```bash
cp .env.example .env
# Open .env and set ANTHROPIC_API_KEY and POSTGRES_URL at minimum.
# All other integrations fall back to mock clients automatically.
```

### 4 — Start the server

```bash
python run.py
```

```
INFO  IRAS graph compiled and ready
INFO  Uvicorn running on http://0.0.0.0:8000
```

### 5 — Fire your first alert

```bash
curl -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d '{
    "title": "High error rate on payment-service",
    "timestamp": "2026-05-03T10:30:00Z",
    "service": "payment-service",
    "error_rate": 0.45
  }'
```

```json
{"incident_id": "550e8400-e29b-41d4-a716-446655440000", "status": "processing"}
```

### 6 — Approve the remediation plan

```bash
curl -X POST http://localhost:8000/incidents/550e8400-e29b-41d4-a716-446655440000/approve
```

The graph resumes, applies the fix, writes the post-mortem, and closes the incident.

---

## How It Works

### 1 · Alert Ingestion

Any JSON webhook with `title` and `timestamp` fields is accepted. Extra fields — Prometheus labels, error messages, stack traces, deployment annotations — are passed through transparently to the AI agents.

A single UUID is pre-generated and used simultaneously as the HTTP response `incident_id`, the LangGraph `thread_id`, and the initial state `incident_id`. Callers always use the same ID returned from the webhook to approve or reject later.

### 2 · Triage (Claude Haiku)

Fast classification: P0–P3 severity, affected services, estimated user impact, and confidence score.

| Severity | Meaning | Auto-escalation after |
|---|---|---|
| P0 | Complete outage | 15 minutes |
| P1 | Major degradation | 2 hours |
| P2 | Partial degradation | 2 hours |
| P3 | Warning / informational | 2 hours |

### 3 · Context Gathering (Claude Haiku + Tools)

The context agent calls three tools and bundles the results into a typed `ContextBundle`:

| Tool | Source | Fetches |
|---|---|---|
| `fetch_logs` | Elasticsearch or Loki | Error/warning lines around the alert time |
| `fetch_metrics` | Prometheus | Current vs. 7-day baseline for relevant metrics |
| `fetch_deployments` | GitHub Deployments API | Service deployments in the last 24 hours |

### 4 · Root Cause Analysis (Claude Sonnet)

Produces a `RootCauseHypothesis` with `primary_cause`, `contributing_factors`, `evidence` (specific log lines), and a `confidence` score (0–1).

**Confidence-gated retry loop:**
- `confidence >= 0.7` → proceed to remediation planning
- `confidence < 0.7`, attempts < `RCA_MAX_ATTEMPTS` → loop back to context-gathering for broader evidence
- attempts exhausted → automatic escalation via PagerDuty

### 5 · Remediation Planning (Claude Sonnet)

Generates a typed `RemediationPlan` with ordered steps, each containing:

```
action             — human-readable description
rollback_command   — exact command to undo this step
risk_level         — low | medium | high
estimated_duration — seconds
```

**Safety invariants enforced regardless of model output:**
- Any step with `risk_level = "high"` → `requires_human_approval` forced to `True`
- Any step with a whitespace-only `rollback_command` → plan marked `reversible = False` and `requires_human_approval = True`

### 6 · Human-in-the-Loop Approval

LangGraph's `interrupt()` pauses the graph. The full state is checkpointed to PostgreSQL. The graph resumes when the on-call engineer calls:

```
POST /incidents/{id}/approve   →  proceeds to apply remediation
POST /incidents/{id}/reject    →  routes to manual escalation
```

A background timeout monitor triggers automatic escalation if no decision arrives within the SLA window.

### 7 · Apply Remediation

Steps are executed sequentially. On failure at any step, all completed steps are rolled back in reverse order using their `rollback_command`.

### 8 · Escalation

Triggered when:
- RCA confidence never reached threshold after `RCA_MAX_ATTEMPTS` retries
- Human rejected the remediation plan
- Approval timeout expired

IRAS fires an idempotent PagerDuty incident and posts a structured Slack message with full context (severity, root cause, RCA attempts, rejection reason).

### 9 · Post-Mortem (Claude Sonnet)

Always runs — whether the incident was resolved or escalated. Produces a `PostMortem` with timeline, root cause summary, resolution summary, and action items. Stored in PostgreSQL and posted to the incident Slack channel.

---

## Configuration

Copy `.env.example` to `.env`:

```bash
cp .env.example .env
```

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | — | Claude API key (`sk-ant-...`) |
| `POSTGRES_URL` | ✅ | — | `postgresql://user:pass@host:5432/db` |
| `SLACK_BOT_TOKEN` | ⬜ | mock | Slack bot token (`xoxb-...`) |
| `SLACK_ONCALL_CHANNEL_ID` | ⬜ | mock | Slack channel ID for on-call alerts |
| `PAGERDUTY_INTEGRATION_KEY` | ⬜ | mock | PagerDuty Events API v2 key |
| `PROMETHEUS_BASE_URL` | ⬜ | mock | `http://prometheus:9090` |
| `ELASTICSEARCH_BASE_URL` | ⬜ | — | Pick one log backend |
| `LOKI_BASE_URL` | ⬜ | — | Pick one log backend |
| `LANGSMITH_API_KEY` | ⬜ | disabled | LangSmith graph tracing |
| `LANGSMITH_PROJECT` | ⬜ | `iras` | LangSmith project name |
| `LOGFIRE_TOKEN` | ⬜ | disabled | Logfire agent tracing |
| `RCA_CONFIDENCE_THRESHOLD` | ⬜ | `0.7` | Min confidence to proceed to planning |
| `RCA_MAX_ATTEMPTS` | ⬜ | `3` | Max RCA retries before escalation |
| `APPROVAL_TIMEOUT_P0_MINUTES` | ⬜ | `15` | P0 approval window in minutes |
| `APPROVAL_TIMEOUT_DEFAULT_MINUTES` | ⬜ | `120` | P1–P3 approval window in minutes |
| `APP_ENV` | ⬜ | `development` | `development` or `production` |
| `LOG_LEVEL` | ⬜ | `INFO` | `DEBUG` / `INFO` / `WARNING` |

> Optional integrations automatically fall back to mock clients when tokens are missing. IRAS runs fully end-to-end with only `ANTHROPIC_API_KEY` + `POSTGRES_URL`.

---

## API Reference

### `GET /health`

```json
{"status": "ok", "env": "development"}
```

---

### `POST /webhook/alert`

Ingests an alert and starts the autonomous response workflow in the background.

**Body** (minimum — all extra fields pass through to the AI agents):
```json
{
  "title": "High error rate on payment-service",
  "timestamp": "2026-05-03T10:30:00Z"
}
```

**Response `202 Accepted`:**
```json
{"incident_id": "550e8400-e29b-41d4-a716-446655440000", "status": "processing"}
```

---

### `POST /incidents/{incident_id}/approve`

Approves the pending remediation plan. Resumes the paused graph.

**Response `200 OK`:**
```json
{"incident_id": "550e8400...", "decision": "approved", "status": "resumed"}
```

---

### `POST /incidents/{incident_id}/reject`

Rejects the plan. Routes to PagerDuty escalation.

**Response `200 OK`:**
```json
{"incident_id": "550e8400...", "decision": "rejected", "status": "resumed"}
```

---

## Running Tests

```bash
# Full suite (292 tests)
pytest -q

# Unit + integration only (fast)
pytest tests/unit tests/integration -q --no-cov

# Stress + adversarial scenarios
pytest tests/stress/ -v --no-cov

# Single scenario class
pytest tests/stress/test_real_world_scenarios.py::TestScenarioP0Outage -v --no-cov

# HTML coverage report
pytest --cov=src/iras --cov-report=html && open htmlcov/index.html
```

**292 tests, 99%+ coverage, 0 failures**

The test suite covers:
- P0–P3 happy paths end-to-end with `MemorySaver`
- RCA retry loops (confidence below threshold for N-1 attempts)
- Human rejection → PagerDuty escalation
- Plan generation failure → skip approval → escalation
- All context tools failing simultaneously (graceful degradation)
- 20 concurrent incidents with zero state contamination
- 50 sequential incidents throughput benchmark
- Adversarial model outputs (model lies about `risk_level`, empty rollback commands)
- Unicode and XSS payloads
- Large payloads (10,000-char titles, 200-item affected services list)
- Bug regression tests (B1–B4)

---

## Observability

| Signal | Tool | What it covers |
|---|---|---|
| Graph traces | LangSmith | Every node: inputs, outputs, timing, token usage |
| Agent traces | Logfire | Every LLM call: prompt, response, tool calls, validation |
| Structured logs | Python `logging` | Every node emits `incident_id`, `node_name`, `timestamp` |
| Post-mortems | PostgreSQL | Full `PostMortem` records queryable by severity, duration |

---

## Deployment

### Docker Compose (recommended)

```yaml
services:
  iras:
    build: .
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy

  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: iras
      POSTGRES_PASSWORD: secret
      POSTGRES_DB: iras
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U iras"]
      interval: 5s
      retries: 5
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

```bash
docker compose up -d
```

### Production Checklist

- [ ] Add authentication to `/incidents/{id}/approve` and `/incidents/{id}/reject` (Slack request signing or OAuth)
- [ ] Set `APP_ENV=production`
- [ ] Configure real Slack + PagerDuty tokens
- [ ] Enable LangSmith and Logfire for production observability
- [ ] Add a reverse proxy (nginx / Caddy) with TLS
- [ ] Set up PgBouncer for Postgres connection pooling

---

## Extending IRAS

### Add a new context tool

1. Implement a client in `src/iras/tools/` with a `MockXClient` fallback
2. Add it to `ContextDeps` in `src/iras/agents/deps.py`
3. Register the `@context_agent.tool` in `src/iras/agents/context_gathering.py`

### Change the AI model

Each agent instantiates its own `pydantic_ai.Agent`. Switch models per-agent:

```python
# Higher accuracy for RCA
rca_agent = Agent(model="claude-opus-4-5", ...)

# Faster/cheaper triage
triage_agent = Agent(model="claude-haiku-3-5", ...)
```

### Add a new notification backend

Both `escalation_node` and `postmortem_node` accept injectable `deps`. Implement the same `post_message` / `trigger_incident` interface and swap it in.

---

## Contributing

1. Fork and create a feature branch
2. Make your changes
3. Run `pytest` — all 292 tests must pass
4. Keep coverage above 98%: `pytest --cov=src/iras --cov-fail-under=98`
5. Open a pull request

---

## License

MIT — see [LICENSE](LICENSE)

---

<div align="center">

Built with [LangGraph](https://langchain-ai.github.io/langgraph/) · [Pydantic AI](https://ai.pydantic.dev) · [FastAPI](https://fastapi.tiangolo.com) · [Claude](https://anthropic.com)

**If IRAS saved your on-call rotation, give it a ⭐**

</div>
