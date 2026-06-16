<div align="center">

# ⚡ IRAS — AI Incident Response Autopilot

### Your on-call engineer gets paged at 3 AM.
### IRAS finds the likely root cause, drafts the remediation plan, and waits for human approval before touching production.

<br/>

[![CI](https://github.com/krishnashakula/IRAS/actions/workflows/ci.yml/badge.svg)](https://github.com/krishnashakula/IRAS/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?style=flat-square&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![LangGraph](https://img.shields.io/badge/LangGraph-0.2%2B-FF6B35?style=flat-square)](https://langchain-ai.github.io/langgraph/)
[![Pydantic AI](https://img.shields.io/badge/Pydantic_AI-latest-E92063?style=flat-square)](https://ai.pydantic.dev)
[![Claude](https://img.shields.io/badge/Claude-Sonnet_%7C_Haiku-blueviolet?style=flat-square&logo=anthropic&logoColor=white)](https://anthropic.com)
[![Tests](https://img.shields.io/badge/tests-292%20passing-brightgreen?style=flat-square)](https://pytest.org)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen?style=flat-square)](https://coverage.readthedocs.io)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

<br/>

**A production-style AI SRE agent with durable human approval, rollback-first remediation, typed agent outputs, and mock integrations that run locally.**

<br/>

[**🚀 2-Minute Demo**](#-2-minute-demo) ·
[**Why It Matters**](#-why-this-project-is-different) ·
[**Architecture**](#-architecture) ·
[**Safety Model**](#-we-dont-trust-the-model) ·
[**API**](#-api-reference) ·
[**Deploy**](#-deployment) ·
[**Contribute**](#-contributing)

<br/>

⭐ **If this saves even one 3 AM incident, star the repo.**

</div>

---

## The 10-second pitch

**IRAS is an AI incident-response system that turns an alert into:**

1. severity classification,
2. log/metric/deployment context,
3. root-cause hypothesis,
4. rollback-aware remediation plan,
5. human approval checkpoint,
6. safe execution,
7. post-mortem.

All through a **9-node LangGraph workflow** with **typed Pydantic models**, **PostgreSQL checkpointing**, and **safety rules enforced in code instead of prompts**.

---

## Why this project is different

Most AI agent demos look impressive until you ask: “Would I let this touch production?”

IRAS is designed around the opposite assumption:

> **The model is useful, but it is not trusted.**

That is why every remediation step needs rollback logic, high-risk plans force approval, low-confidence RCA loops back for more evidence, and the graph can pause safely while a human makes the final call.

**Built for the real incident lifecycle:**

| Problem in real on-call | What IRAS does |
|---|---|
| Alert fatigue | Triage agent estimates severity, blast radius, and confidence |
| Context switching | Pulls logs, metrics, and deployments in parallel |
| Guessy RCA | Produces evidence-backed root-cause hypotheses |
| Unsafe AI actions | Enforces rollback + approval invariants in code |
| Human bottleneck | Sends approval request and pauses with durable state |
| Forgotten post-mortems | Writes a structured post-mortem automatically |

---

## 2-Minute Demo

> No Slack token? No PagerDuty key? No problem. IRAS falls back to mock clients. You only need an Anthropic API key and Postgres.

```bash
# 1. Clone
git clone https://github.com/krishnashakula/IRAS.git
cd IRAS

# 2. Start Postgres
docker run -d --name iras-postgres \
  -e POSTGRES_USER=iras \
  -e POSTGRES_PASSWORD=secret \
  -e POSTGRES_DB=iras \
  -p 5432:5432 \
  postgres:16

# 3. Install
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 4. Configure
cp .env.example .env
# Set ANTHROPIC_API_KEY and POSTGRES_URL

# 5. Run
python run.py
```

Expected output:

```bash
INFO  IRAS graph compiled and ready
INFO  Uvicorn running on http://0.0.0.0:8000
```

Fire a fake production incident:

```bash
curl -X POST http://localhost:8000/webhook/alert \
  -H "Content-Type: application/json" \
  -d '{
    "title": "High error rate on payment-service",
    "timestamp": "2026-05-03T10:30:00Z",
    "service": "payment-service",
    "error_rate": 0.45,
    "region": "us-east-1"
  }'
```

Example response:

```json
{
  "incident_id": "550e8400-...",
  "status": "processing"
}
```

Approve the remediation plan:

```bash
curl -X POST http://localhost:8000/incidents/550e8400-.../approve
```

---

## What happens after an alert?

```mermaid
flowchart LR
    A[Alert fires] --> B[Triage]
    B --> C[Gather logs + metrics + deployments]
    C --> D[Root cause analysis]
    D --> E{Confidence >= 0.7?}
    E -- No --> C
    E -- Yes --> F[Generate remediation plan]
    F --> G{Human approval}
    G -- Approve --> H[Apply remediation]
    G -- Reject --> I[Escalate]
    H --> J[Post-mortem]
    I --> J
```

---

## How It Works

IRAS runs a **9-node LangGraph state machine**. Each stage produces a typed Pydantic model — no raw model strings, no fragile output parsing.

```text
Alert → Triage → Context → RCA → Plan → [YOU] → Apply → Post-mortem
                    ↑         ↓
                    └── retry if confidence < 0.7
```

### Stage 1 — Ingest

Accepts any JSON webhook with `title` and `timestamp`.

Supported sources include:

- PagerDuty
- Prometheus AlertManager
- Datadog
- Grafana
- raw `curl`
- any JSON webhook

Extra fields pass through to the agents as incident context.

### Stage 2 — Triage

Uses **Claude Haiku** for fast, low-cost classification:

- P0–P3 severity
- affected services
- blast radius
- confidence score

### Stage 3 — Context Gathering

Runs three tool calls in parallel:

- `fetch_logs` → error/warning lines from Elasticsearch or Loki
- `fetch_metrics` → current vs. 7-day Prometheus baseline
- `fetch_deployments` → recent GitHub Deployments for the affected service

### Stage 4 — Root Cause Analysis

Uses **Claude Sonnet** to produce a typed `RootCauseHypothesis`:

- primary cause
- contributing factors
- evidence
- confidence

If confidence is below `0.7`, IRAS automatically loops back for a wider context window.

### Stage 5 — Remediation Planning

Produces a rollback-aware `RemediationPlan`:

- ordered steps
- exact commands
- rollback commands
- risk levels
- estimated durations

### Stage 6 — Human Approval

LangGraph `interrupt()` pauses the graph.

The full incident state is checkpointed in PostgreSQL, so the process can restart and resume from the same approval step.

### Stage 7 — Apply Remediation

Steps execute sequentially.

If one step fails, completed steps roll back in reverse order using their stored rollback commands.

### Stage 8 — Post-mortem

Writes a structured post-mortem regardless of outcome:

- timeline
- root cause
- resolution
- action items
- final status

---

## We Don't Trust the Model

Most AI agent projects trust model output at face value.

IRAS does not.

**Safety invariants are enforced in code, not prompts:**

```python
# The model cannot generate an unsafe plan that bypasses approval.
# These checks run regardless of what the model returns.

if any(step.risk_level == "high" for step in plan.steps):
    plan.requires_human_approval = True

if any(not step.rollback_command.strip() for step in plan.steps):
    plan.reversible = False
    plan.requires_human_approval = True
```

### Tested against adversarial behavior

IRAS includes tests for:

- model lies about risk level
- model returns empty rollback commands
- all context tools fail simultaneously
- 20 concurrent incidents
- Unicode/XSS/oversized payloads
- RCA confidence never reaches threshold
- rejected remediation plans
- approval timeouts

```bash
pytest -q
pytest tests/stress/ -v --no-cov
pytest --cov=src/iras --cov-report=html
```

Current proof points:

- **292 tests passing**
- **99% coverage**
- **47 stress/adversarial scenarios**
- **PostgreSQL durable checkpointing**
- **Mock integrations for local demos**

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

The most technically interesting part of IRAS is the approval step.

Many agent systems fake human-in-the-loop with polling, queues, or timeouts. IRAS uses LangGraph `interrupt()` so the workflow genuinely pauses mid-execution.

```python
# The graph pauses here.
# State is checkpointed in Postgres.
human_decision = interrupt({"message": "Approve remediation plan?"})

# The graph resumes here after approval/rejection.
if human_decision["approved"]:
    return apply_remediation(state)
else:
    return escalate(state)
```

That means:

- server restarts do not lose the incident,
- deployments do not lose the approval state,
- multiple incidents do not contaminate each other,
- the system resumes from the exact checkpoint.

---

## Project Structure

```text
IRAS/
├── src/iras/
│   ├── api/
│   │   ├── app.py
│   │   ├── background.py
│   │   └── routes/
│   │       ├── webhook.py
│   │       └── approval.py
│   │
│   ├── graph/
│   │   ├── builder.py
│   │   ├── checkpointer.py
│   │   ├── state.py
│   │   └── nodes/
│   │       ├── ingestion.py
│   │       ├── triage.py
│   │       ├── context_gathering.py
│   │       ├── rca.py
│   │       ├── generate_plan.py
│   │       ├── approval.py
│   │       ├── apply_remediation.py
│   │       ├── escalation.py
│   │       └── postmortem.py
│   │
│   ├── agents/
│   ├── models/
│   ├── tools/
│   └── config/settings.py
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── stress/
```

---

## API Reference

### `POST /webhook/alert`

Accepts any JSON with `title` and `timestamp`.

```json
{
  "title": "High error rate on payment-service",
  "timestamp": "2026-05-03T10:30:00Z"
}
```

Response:

```json
{
  "incident_id": "550e8400-...",
  "status": "processing"
}
```

### `POST /incidents/{id}/approve`

Resumes the paused graph and applies the remediation plan.

### `POST /incidents/{id}/reject`

Rejects the plan and routes the incident to escalation.

### `GET /health`

```json
{
  "status": "ok",
  "env": "development"
}
```

---

## Configuration

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---:|---|
| `ANTHROPIC_API_KEY` | ✅ | Claude API key |
| `POSTGRES_URL` | ✅ | `postgresql://user:pass@host:5432/db` |
| `SLACK_BOT_TOKEN` | Optional | Falls back to mock client if unset |
| `SLACK_ONCALL_CHANNEL_ID` | Optional | Slack channel for on-call alerts |
| `PAGERDUTY_INTEGRATION_KEY` | Optional | Falls back to mock client if unset |
| `PROMETHEUS_BASE_URL` | Optional | Falls back to mock client if unset |
| `ELASTICSEARCH_BASE_URL` | Optional | Pick one log backend |
| `LOKI_BASE_URL` | Optional | Pick one log backend |
| `LANGSMITH_API_KEY` | Optional | LangSmith graph tracing |
| `LOGFIRE_TOKEN` | Optional | Logfire agent tracing |
| `RCA_CONFIDENCE_THRESHOLD` | Optional | Default: `0.7` |
| `RCA_MAX_ATTEMPTS` | Optional | Default: `3` |
| `APPROVAL_TIMEOUT_P0_MINUTES` | Optional | Default: `15` |
| `APPROVAL_TIMEOUT_DEFAULT_MINUTES` | Optional | Default: `120` |

---

## Observability

| Signal | Tool | What it covers |
|---|---|---|
| Graph traces | LangSmith | Node inputs, outputs, timing, token usage |
| Agent traces | Logfire | LLM calls, tool calls, validation |
| Structured logs | Python logging | `incident_id`, `node_name`, timestamps |
| Post-mortems | PostgreSQL | Severity, duration, root cause, final outcome |

---

## Deployment

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

### Production checklist

- [ ] Add auth to `/approve` and `/reject`
- [ ] Verify Slack request signing or OAuth
- [ ] Set `APP_ENV=production`
- [ ] Configure real Slack + PagerDuty tokens
- [ ] Enable LangSmith + Logfire
- [ ] Add reverse proxy with TLS
- [ ] Add PgBouncer for Postgres connection pooling
- [ ] Add allowlisted remediation commands
- [ ] Add RBAC for approval actions
- [ ] Add audit logging for every human decision

---

## Roadmap

- [ ] Web UI for incident timeline and approval
- [ ] GitHub Actions demo workflow
- [ ] Kubernetes remediation executor
- [ ] Terraform drift detection context tool
- [ ] OpenTelemetry traces as RCA evidence
- [ ] Policy engine for allowed remediation commands
- [ ] Multi-tenant incident workspace
- [ ] Incident replay mode for demos and testing

---

## Who this is for

IRAS is useful if you are building or researching:

- AI SRE agents
- production LLM workflows
- LangGraph human-in-the-loop systems
- safe AI automation
- agentic DevOps tools
- incident response copilots
- typed AI agent architectures
- approval-gated remediation systems

---

## Built with

- **LangGraph** — durable graph workflow and interrupt/resume
- **Pydantic AI** — typed agent outputs and tool interfaces
- **FastAPI** — incident and approval API
- **PostgreSQL** — durable checkpointing and post-mortems
- **Claude Haiku** — fast triage and context gathering
- **Claude Sonnet** — deeper RCA, remediation, and post-mortem generation
- **Prometheus / Loki / Elasticsearch / GitHub / Slack / PagerDuty** — real integrations with mock fallbacks

---

## Contributing

Contributions are welcome.

Good first issues:

- add a new context tool,
- improve mock incident scenarios,
- add a Kubernetes executor,
- improve the post-mortem template,
- add a frontend timeline view,
- add more adversarial safety tests.

```bash
git clone https://github.com/krishnashakula/IRAS.git
cd IRAS
pytest
```

Before opening a PR:

- [ ] all tests pass,
- [ ] coverage remains above 98%,
- [ ] unsafe remediation paths have tests,
- [ ] new integrations include mock fallbacks,
- [ ] docs include a runnable example.

---

## GitHub growth checklist for this repo

Use this checklist before posting publicly:

### Repo polish

- [ ] Add a 60–90 second GIF demo at the top of this README.
- [ ] Add a real screenshot of the approval message.
- [ ] Add a real screenshot of the generated post-mortem.
- [ ] Add GitHub topics:
  - `ai-agent`
  - `sre`
  - `incident-response`
  - `langgraph`
  - `pydantic-ai`
  - `fastapi`
  - `devops`
  - `observability`
  - `llmops`
  - `human-in-the-loop`
- [ ] Add a pinned demo issue: “Try IRAS locally in 2 minutes.”
- [ ] Add a `good first issue` label.
- [ ] Add a `demo` folder with sample alerts and expected outputs.
- [ ] Add a `SECURITY.md` explaining why approvals and rollback gates matter.
- [ ] Add a `CONTRIBUTING.md` with 3 easy contribution paths.
- [ ] Add an architecture image preview for social cards.

### Launch post angles

Use one clear story, not a generic “I built a project” post.

Best angles:

1. **“I built an AI agent I would actually trust near production.”**
2. **“Most AI SRE demos skip the dangerous part: approval and rollback.”**
3. **“I built a LangGraph incident-response agent that pauses for human approval.”**
4. **“The model can suggest the fix. It cannot bypass safety.”**

### Example launch post

```text
I built IRAS: an AI incident-response autopilot for 3 AM production alerts.

It takes an alert and generates:
• severity triage
• logs/metrics/deployment context
• root-cause hypothesis
• rollback-aware remediation plan
• human approval checkpoint
• post-mortem

The key design choice:
The model is useful, but it is not trusted.

High-risk plans force approval.
Missing rollback commands block automation.
Low-confidence RCA loops back for more evidence.
The LangGraph workflow pauses with PostgreSQL checkpointing and resumes after approval.

Stack:
LangGraph · Pydantic AI · FastAPI · PostgreSQL · Claude · Prometheus/Loki/GitHub/Slack/PagerDuty mocks

Repo: https://github.com/krishnashakula/IRAS

If you build AI agents, SRE tools, or LLM automation, I would love feedback.
```

### Reddit / Hacker News title ideas

```text
Show HN: IRAS — AI incident response agent with human approval and rollback gates
```

```text
I built an AI SRE agent that does RCA but cannot bypass human approval
```

```text
LangGraph + FastAPI agent for 3 AM incident response, with durable approval checkpoints
```

### Star CTA

Use this line consistently:

```text
If this is the kind of AI automation you want in production, give it a star ⭐
```

---

## License

MIT — see [LICENSE](LICENSE)

---

<div align="center">

Built with LangGraph · Pydantic AI · FastAPI · Claude

<br/>

**If IRAS handled your 3 AM incident, give it a ⭐**

</div>
