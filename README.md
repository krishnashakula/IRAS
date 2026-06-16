<div align="center">

# ⚡ IRAS

### Your on-call engineer gets woken up at 3 AM.
### IRAS already found the root cause, wrote a remediation plan, and is waiting for your approval.

<br/>

[![CI](https://github.com/krishnashakula/IRAS/actions/workflows/ci.yml/badge.svg)](https://github.com/krishnashakula/IRAS/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-FF6B35?style=flat-square)](https://langchain-ai.github.io/langgraph/)
[![Pydantic AI](https://img.shields.io/badge/Pydantic_AI-latest-E92063?style=flat-square)](https://ai.pydantic.dev)
[![Claude](https://img.shields.io/badge/Claude-Sonnet_%7C_Haiku-blueviolet?style=flat-square&logo=anthropic&logoColor=white)](https://anthropic.com)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen?style=flat-square)](https://coverage.readthedocs.io)
[![Tests](https://img.shields.io/badge/tests-292%20passing-brightgreen?style=flat-square)](https://pytest.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

<br/>

[**Quick Start**](#-quick-start) · [**How It Works**](#-how-it-works) · [**We Don't Trust the Model**](#-we-dont-trust-the-model) · [**Architecture**](#-architecture) · [**Configuration**](#-configuration) · [**Contributing**](#-contributing)

</div>

---

## The Problem

You've been there. 3 AM. PagerDuty fires. You stumble to your laptop, squint at a graph, dig through logs, cross-reference a recent deployment, form a hypothesis, write a Slack message, wait for approval, apply a fix, then spend an hour writing a post-mortem that nobody reads.

Every single time.

IRAS does all of that — automatically, in under 2 minutes — and only wakes you up to press Approve.

---

## Quick Start

> No Slack token? No PagerDuty key? No problem. Everything falls back to mock clients. You only need an Anthropic API key and Docker.

```bash
# 1. Clone
git clone https://github.com/krishnashakula/IRAS.git && cd IRAS

# 2. Start Postgres
docker run -d --name iras-postgres \
  -e POSTGRES_USER=iras -e POSTGRES_PASSWORD=secret -e POSTGRES_DB=iras \
  -p 5432:5432 postgres:16

# 3. Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 4. Configure (only two fields required)
cp .env.example .env
# Set ANTHROPIC_API_KEY and POSTGRES_URL

# 5. Run
python run.py
```

```
INFO  IRAS graph compiled and ready
INFO  Uvicorn running on http://0.0.0.0:8000
```

```bash
# Fire an alert
curl -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d '{
    "title": "High error rate on payment-service",
    "timestamp": "2026-05-03T10:30:00Z",
    "service": "payment-service",
    "error_rate": 0.45
  }'

# → {"incident_id": "550e8400-...", "status": "processing"}

# Watch it work in your terminal, then approve
curl -X POST http://localhost:8000/incidents/550e8400-.../approve
```

---

## How It Works

IRAS runs a **9-node LangGraph state machine**. Each stage produces a typed Pydantic model — no raw strings, no prompt output you have to parse.

```
Alert → Triage → Context → RCA → Plan → [YOU] → Apply → Post-mortem
                    ↑         ↓
                    └── retry if confidence < 0.7
```

**Stage 1 — Ingest**: Any JSON webhook with `title` + `timestamp` is accepted. PagerDuty, Prometheus AlertManager, Datadog, Grafana — or a raw `curl`. Extra fields pass straight through to the AI.

**Stage 2 — Triage** *(Claude Haiku)*: P0–P3 severity, affected services, estimated blast radius, confidence score. Fast and cheap — Haiku is used here intentionally.

**Stage 3 — Context Gathering** *(Claude Haiku + tool calls)*: Three parallel tool calls:
- `fetch_logs` → error/warning lines from Elasticsearch or Loki
- `fetch_metrics` → current vs. 7-day baseline from Prometheus
- `fetch_deployments` → recent GitHub Deployments for the affected service

**Stage 4 — Root Cause Analysis** *(Claude Sonnet)*: Produces a `RootCauseHypothesis` with `primary_cause`, `contributing_factors`, `evidence` (specific log lines), and a `confidence` score.

Confidence gate: if score < 0.7, the graph loops back to context-gathering for a broader evidence window. After `RCA_MAX_ATTEMPTS`, it escalates automatically.

**Stage 5 — Remediation Planning** *(Claude Sonnet)*: Ordered steps with human-readable descriptions, exact rollback commands, risk levels, and estimated durations.

**Stage 6 — Human Approval** *(you)*: LangGraph's `interrupt()` pauses the graph. State is checkpointed to PostgreSQL — the server can restart and the incident survives. You get a Slack message with Approve / Reject buttons, or hit the API directly.

**Stage 7 — Apply Remediation**: Steps execute sequentially. On failure, completed steps roll back in reverse order using their stored `rollback_command`.

**Stage 8 — Post-mortem** *(Claude Sonnet)*: Timeline, root cause, resolution, action items. Written regardless of outcome — resolved or escalated. Stored in PostgreSQL, posted to Slack.

---

## We Don't Trust the Model

Most AI agent projects trust the model's output at face value. IRAS doesn't.

**Safety invariants enforced in code, not prompts:**

```python
# The model cannot generate an unsafe plan that bypasses approval.
# These checks run regardless of what the model returns.

if any(step.risk_level == "high" for step in plan.steps):
    plan.requires_human_approval = True          # forced

if any(not step.rollback_command.strip() for step in plan.steps):
    plan.reversible = False                       # forced
    plan.requires_human_approval = True          # forced
```

**292 tests, 99% coverage** — including adversarial scenarios specifically designed to test model misbehavior:

- Model lies about `risk_level` → safety override catches it
- Model returns empty `rollback_command` → plan blocked
- All context tools fail simultaneously → graceful degradation, not crash
- 20 concurrent incidents → zero state contamination
- Unicode, XSS, 10,000-character payloads → handled cleanly
- Model confidence never reaches threshold → automatic PagerDuty escalation

```bash
pytest -q                          # 292 tests, ~30s
pytest tests/stress/ -v --no-cov  # adversarial scenarios
pytest --cov=src/iras --cov-report=html  # 99% coverage report
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

### The Interrupt Pattern

The most technically interesting part of IRAS is how it handles the human-in-the-loop approval step.

Most agent frameworks fake this with polling or timeouts. LangGraph's `interrupt()` is different: the graph **genuinely pauses mid-execution**, serializes its entire state to PostgreSQL, and resumes from exactly that point when the human responds — even across server restarts, deployments, or process crashes.

```python
# The graph pauses here. State is in Postgres.
# The server can restart. The incident is safe.
human_decision = interrupt({"message": "Approve remediation plan?"})

# Resumes here when POST /incidents/{id}/approve is called.
if human_decision["approved"]:
    return apply_remediation(state)
else:
    return escalate(state)
```

This is why IRAS is built on LangGraph and not a simpler framework. Durable execution matters for production incident response.

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
│   │       ├── ingestion.py
│   │       ├── triage.py             # → Claude Haiku
│   │       ├── context_gathering.py  # → Claude Haiku + tool calls
│   │       ├── rca.py                # → Claude Sonnet + retry routing
│   │       ├── generate_plan.py      # → Claude Sonnet + Slack notify
│   │       ├── approval.py           # interrupt() — durable human checkpoint
│   │       ├── apply_remediation.py  # Execute steps + rollback on failure
│   │       ├── escalation.py         # PagerDuty + Slack
│   │       └── postmortem.py         # → Claude Sonnet + persist to DB
│   │
│   ├── agents/                       # One Pydantic AI agent per stage
│   ├── models/                       # TriageResult · ContextBundle · RootCauseHypothesis
│   │                                 # RemediationPlan · RemediationStep · PostMortem
│   ├── tools/                        # Elasticsearch · Loki · Prometheus · GitHub · Slack · PagerDuty
│   └── config/settings.py            # Pydantic Settings — reads .env
│
├── tests/
│   ├── unit/                         # Fully mocked
│   ├── integration/                  # Live service tests (opt-in)
│   ├── e2e/                          # Full graph runs with MemorySaver
│   └── stress/                       # 47 adversarial + real-world scenarios
```

---

## Severity & Escalation

| Severity | Meaning | Approval window |
|---|---|---|
| P0 | Complete outage | 15 minutes, then auto-escalate |
| P1 | Major degradation | 2 hours |
| P2 | Partial degradation | 2 hours |
| P3 | Warning / informational | 2 hours |

Escalation is triggered when: RCA confidence never reaches threshold after max retries · Human rejects the plan · Approval timeout expires.

On escalation: idempotent PagerDuty incident fires + structured Slack message with full context. Post-mortem always runs — resolved or not.

---

## Configuration

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key (`sk-ant-...`) |
| `POSTGRES_URL` | ✅ | `postgresql://user:pass@host:5432/db` |
| `SLACK_BOT_TOKEN` | ⬜ | Falls back to mock client if unset |
| `SLACK_ONCALL_CHANNEL_ID` | ⬜ | Slack channel for on-call alerts |
| `PAGERDUTY_INTEGRATION_KEY` | ⬜ | Falls back to mock client if unset |
| `PROMETHEUS_BASE_URL` | ⬜ | Falls back to mock client if unset |
| `ELASTICSEARCH_BASE_URL` | ⬜ | Pick one log backend |
| `LOKI_BASE_URL` | ⬜ | Pick one log backend |
| `LANGSMITH_API_KEY` | ⬜ | LangSmith graph tracing |
| `LOGFIRE_TOKEN` | ⬜ | Logfire agent tracing |
| `RCA_CONFIDENCE_THRESHOLD` | ⬜ | Default: `0.7` |
| `RCA_MAX_ATTEMPTS` | ⬜ | Default: `3` |
| `APPROVAL_TIMEOUT_P0_MINUTES` | ⬜ | Default: `15` |
| `APPROVAL_TIMEOUT_DEFAULT_MINUTES` | ⬜ | Default: `120` |

> **All integrations fall back to mock clients when tokens are absent.** IRAS runs fully end-to-end with only `ANTHROPIC_API_KEY` + `POSTGRES_URL`.

---

## API Reference

### `POST /webhook/alert`

Accepts any JSON with `title` + `timestamp`. All extra fields pass through to the AI agents.

```json
{ "title": "High error rate on payment-service", "timestamp": "2026-05-03T10:30:00Z" }
```
```json
{ "incident_id": "550e8400-...", "status": "processing" }
```

### `POST /incidents/{id}/approve`
### `POST /incidents/{id}/reject`

Resumes the paused graph. Approve routes to remediation. Reject routes to PagerDuty escalation.

### `GET /health`

```json
{ "status": "ok", "env": "development" }
```

---

## Observability

| Signal | Tool | What it covers |
|---|---|---|
| Graph traces | LangSmith | Every node: inputs, outputs, timing, token usage |
| Agent traces | Logfire | Every LLM call: prompt, response, tool calls, validation |
| Structured logs | Python `logging` | Every node emits `incident_id`, `node_name`, `timestamp` |
| Post-mortems | PostgreSQL | Full records queryable by severity, duration, outcome |

---

## Deployment

```yaml
# docker-compose.yml
services:
  iras:
    build: .
    ports: ["8000:8000"]
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

**Production checklist:**
- [ ] Add auth to `/approve` and `/reject` (Slack request signing or OAuth)
- [ ] Set `APP_ENV=production`
- [ ] Configure real Slack + PagerDuty tokens
- [ ] Enable LangSmith + Logfire
- [ ] Add reverse proxy (nginx / Caddy) with TLS
- [ ] Set up PgBouncer for Postgres connection pooling

---

## Extending IRAS

**Add a context tool**: Implement a client in `src/iras/tools/` with a `MockXClient` fallback → add to `ContextDeps` → register `@context_agent.tool`.

**Swap models per agent**: Each agent instantiates its own `pydantic_ai.Agent`.

```python
rca_agent = Agent(model="claude-opus-4-5", ...)    # higher accuracy
triage_agent = Agent(model="claude-haiku-3-5", ...)  # faster/cheaper
```

**Add a notification backend**: Both `escalation_node` and `postmortem_node` accept injectable deps. Implement `post_message` / `trigger_incident` and swap it in.

---

## Contributing

1. Fork and create a feature branch
2. Run `pytest` — all 292 tests must pass
3. Keep coverage above 98%: `pytest --cov=src/iras --cov-fail-under=98`
4. Open a pull request

---

## License

MIT — see [LICENSE](LICENSE)

---

<div align="center">

Built with [LangGraph](https://langchain-ai.github.io/langgraph/) · [Pydantic AI](https://ai.pydantic.dev) · [FastAPI](https://fastapi.tiangolo.com) · [Claude](https://anthropic.com)

<br/>

**If IRAS handled your 3 AM incident, give it a ⭐**

</div>
