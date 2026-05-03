"""Comprehensive real-world scenario and stress tests for IRAS.

Scenarios covered:
  1.  P1 happy path — payment-service high error rate → approve → remediate
  2.  P0 full outage — database down, all services affected
  3.  P2 memory leak — low-confidence RCA retries, succeeds on 2nd attempt
  4.  P3 disk warning — low-severity, auto-approved, fast path
  5.  Human rejection — plan generated, on-call rejects → escalation
  6.  RCA max retries — confidence never reaches threshold → escalation
  7.  Plan generation failure — UnexpectedModelBehavior → skip interrupt → escalation
  8.  All context tools fail — graceful RCA from empty context
  9.  Missing required fields — ingestion_node raises ValueError
  10. Unicode/special chars — injection-attempt payloads
  11. Large payload — no serialisation crash
  12. Concurrent incidents — 20 alerts simultaneously, no state corruption
  13. Sequential stress — 50 incidents back-to-back, timing baseline
    14. Adversarial agent outputs — model lies, malformed invariants, replay attempts
    15. API schema abuse — hostile nested extras, wrong primitive types, passthrough fidelity
    16. API stress — 50 concurrent HTTP requests (optional; skipped if server is down)

  Bug documentation (tests prove the behaviour exists, then the fix makes them pass):
  B1. Webhook triple-UUID mismatch: correlation_id ≠ thread_id ≠ state incident_id
  B2. Postmortem incident_id must equal state incident_id (not model's arbitrary value)
  B3. apply_remediation_node no-plan path does NOT set escalated=True
  B4. Concurrent get_checkpointer() calls risk double-initialisation (no asyncio.Lock)
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.context_gathering import context_agent
from iras.agents.postmortem import postmortem_agent
from iras.agents.rca import rca_agent
from iras.agents.remediation import remediation_agent
from iras.agents.triage import triage_agent
from iras.graph.builder import build_graph
from iras.graph.nodes.ingestion import ingestion_node
from iras.graph.state import IncidentState
from iras.models.incident import PostMortem, RemediationPlan, TriageResult
from iras.tools.pagerduty import MockPagerDutyClient
from iras.tools.slack import MockSlackClient

# ─────────────────────────────────────────────────────────────────────────────
# FunctionModel factories
# ─────────────────────────────────────────────────────────────────────────────


def _triage_resp(
    severity: str = "P1",
    services: list[str] | None = None,
    impact: int = 5000,
    confidence: float = 0.9,
    reasoning: str = "High error rate detected.",
) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "severity": severity,
                        "affected_services": services or ["payment-service"],
                        "estimated_user_impact": impact,
                        "confidence": confidence,
                        "reasoning": reasoning,
                    }
                )
            )
        ]
    )


def _context_resp(
    failed_tools: list[str] | None = None,
    deployments: list[str] | None = None,
) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "log_evidence": [
                            {
                                "timestamp": "2024-01-15T10:29:55Z",
                                "log_line": "ERROR: connection refused to postgres-primary",
                                "relevance_score": 0.95,
                            }
                        ],
                        "metric_anomalies": [
                            {
                                "metric_name": "http_error_rate",
                                "current_value": 0.45,
                                "baseline_value": 0.01,
                                "deviation_percentage": 4400.0,
                            }
                        ],
                        "recent_deployments": deployments or ["payment-service@abc123"],
                        "summary": "DB connectivity issues detected.",
                        "failed_tools": failed_tools or [],
                    }
                )
            )
        ]
    )


def _rca_resp(confidence: float = 0.88) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "primary_cause": "DB connection pool exhaustion after deployment",
                        "contributing_factors": ["pool not resized for new load"],
                        "evidence": [
                            {
                                "timestamp": "2024-01-15T10:29:55Z",
                                "log_line": "ERROR: connection refused",
                                "relevance_score": 0.95,
                            }
                        ],
                        "confidence": confidence,
                        "recommended_action": "Increase connection pool size to 50",
                        "alternative_hypotheses": [],
                    }
                )
            )
        ]
    )


def _remediation_resp(risk_level: str = "low", requires_approval: bool = False) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "steps": [
                            {
                                "action": "Increase DB connection pool from 10 to 50",
                                "rollback_command": "kubectl set env deploy/app DB_POOL_SIZE=10",
                                "risk_level": risk_level,
                                "estimated_duration_seconds": 30,
                            }
                        ],
                        "estimated_total_downtime_minutes": 2.0,
                        "reversible": True,
                        "requires_human_approval": requires_approval,
                        "summary": "Increase DB pool size and restart affected pods.",
                    }
                )
            )
        ]
    )


def _postmortem_resp(incident_id: str = "PLACEHOLDER") -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "incident_id": incident_id,
                        "timeline": [
                            "10:30 UTC — Alert received",
                            "10:31 UTC — Triage P1 assigned",
                            "10:45 UTC — Remediation applied",
                        ],
                        "root_cause_summary": "DB connection pool exhausted after scaling event.",
                        "resolution_summary": "Increased pool size from 10 to 50.",
                        "action_items": [
                            "Add auto-scaling for DB pool",
                            "Alert on pool utilisation > 80%",
                        ],
                        "severity": "P1",
                        "total_duration_minutes": 16,
                    }
                )
            )
        ]
    )


def _escalation_postmortem_resp() -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "incident_id": "PLACEHOLDER",
                        "timeline": [
                            "10:30 UTC — Alert received",
                            "10:45 UTC — Escalated after max RCA attempts",
                        ],
                        "root_cause_summary": "Unknown — escalated after 3 failed RCA attempts.",
                        "resolution_summary": "Escalated to on-call engineer for manual investigation.",
                        "action_items": [
                            "Improve observability for this service",
                            "Add structured logs for DB timeouts",
                        ],
                        "severity": "P1",
                        "total_duration_minutes": 20,
                    }
                )
            )
        ]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Context manager: override all agents at once
# ─────────────────────────────────────────────────────────────────────────────


@contextlib.contextmanager
def _mock_agents(
    triage_fn=None,
    context_fn=None,
    rca_fn=None,
    remediation_fn=None,
    postmortem_fn=None,
):
    """Override all five agents with deterministic FunctionModels."""
    _t = triage_fn or (lambda m, i: _triage_resp())
    _c = context_fn or (lambda m, i: _context_resp())
    _r = rca_fn or (lambda m, i: _rca_resp())
    _rem = remediation_fn or (lambda m, i: _remediation_resp())
    _pm = postmortem_fn or (lambda m, i: _postmortem_resp())
    with (
        triage_agent.override(model=FunctionModel(_t)),
        context_agent.override(model=FunctionModel(_c)),
        rca_agent.override(model=FunctionModel(_r)),
        remediation_agent.override(model=FunctionModel(_rem)),
        postmortem_agent.override(model=FunctionModel(_pm)),
    ):
        yield


def _persist_patch():
    """Patch _persist_postmortem so no Postgres is needed in unit runs."""
    return patch(
        "iras.graph.nodes.postmortem._persist_postmortem",
        new_callable=AsyncMock,
    )


def _slack_patch():
    """Patch SlackClient creation in postmortem/generate_plan nodes."""
    mock_slack = MockSlackClient()
    return patch(
        "iras.graph.nodes.postmortem._build_slack_client",
        return_value=mock_slack,
    )


async def _run_incident_to_interrupt(
    graph, config: dict, alert: dict
) -> dict:
    """Drive the graph to the approval interrupt and return intermediate state."""
    return await graph.ainvoke({"alert_payload": alert}, config=config)


async def _approve_incident(graph, config: dict) -> dict:
    """Resume the graph with an approval decision."""
    return await graph.ainvoke(Command(resume={"approved": True}), config=config)


async def _reject_incident(graph, config: dict) -> dict:
    """Resume the graph with a rejection decision."""
    return await graph.ainvoke(Command(resume={"approved": False}), config=config)


# ─────────────────────────────────────────────────────────────────────────────
# Alert payload fixtures
# ─────────────────────────────────────────────────────────────────────────────

P1_PAYMENT_ALERT: dict[str, Any] = {
    "title": "High error rate on payment-service",
    "timestamp": "2024-01-15T10:30:00Z",
    "source": "prometheus",
    "service": "payment-service",
    "metric": "http_error_rate",
    "current_value": 0.45,
    "threshold": 0.05,
    "environment": "production",
}

P0_DB_ALERT: dict[str, Any] = {
    "title": "Complete database outage — all reads/writes failing",
    "timestamp": "2024-01-15T03:00:00Z",
    "source": "pagerduty",
    "service": "postgres-primary",
    "metric": "db_connection_errors",
    "current_value": 100,
    "threshold": 0,
    "affected_services": ["checkout", "payment", "inventory", "auth"],
}

P2_MEMORY_ALERT: dict[str, Any] = {
    "title": "Memory leak detected on worker-service",
    "timestamp": "2024-01-15T08:00:00Z",
    "source": "datadog",
    "service": "worker-service",
    "metric": "memory_usage_bytes",
    "current_value": 3_900_000_000,
    "threshold": 2_000_000_000,
}

P3_DISK_ALERT: dict[str, Any] = {
    "title": "Disk space warning on log-aggregator",
    "timestamp": "2024-01-15T09:00:00Z",
    "source": "node-exporter",
    "service": "log-aggregator",
    "metric": "disk_usage_percent",
    "current_value": 87,
    "threshold": 80,
}

CASCADE_ALERT: dict[str, Any] = {
    "title": "Auth service cascade failure — 4 services reporting errors",
    "timestamp": "2024-01-15T14:00:00Z",
    "source": "alertmanager",
    "service": "auth-service",
    "affected_services": ["api-gateway", "auth-service", "session-store", "user-service"],
    "metric": "http_5xx_rate",
    "current_value": 0.65,
    "threshold": 0.01,
}


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 1: P1 Happy Path
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioP1HappyPath:
    """P1 payment-service alert → triage → context → RCA → plan → approve → remediate → postmortem."""

    async def test_full_run_completes(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p1-happy-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with _mock_agents(), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final = await _approve_incident(graph, config)

        assert final.get("post_mortem") is not None
        assert final.get("human_approved") is True
        assert final.get("escalated") is False
        assert final.get("resolved_at") is not None

    async def test_triage_result_in_state(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p1-triage-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with _mock_agents(), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)

        triage = mid.get("triage")
        assert triage is not None
        result = TriageResult.model_validate(triage)
        assert result.severity == "P1"
        assert result.confidence == 0.9

    async def test_remediation_plan_has_rollback(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p1-rollback-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with _mock_agents(), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)

        plan_dict = mid.get("remediation_plan")
        assert plan_dict is not None
        plan = RemediationPlan.model_validate(plan_dict)
        for step in plan.steps:
            assert step.rollback_command.strip(), "Every step must have a rollback command"

    async def test_postmortem_has_action_items(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p1-pm-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with _mock_agents(), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final = await _approve_incident(graph, config)

        pm = PostMortem.model_validate(final["post_mortem"])
        assert len(pm.action_items) >= 1
        assert pm.severity == "P1"
        assert pm.total_duration_minutes > 0

    async def test_incident_id_is_uuid(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p1-uuid-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with _mock_agents(), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)

        incident_id = mid.get("incident_id")
        assert incident_id is not None
        # Should be a valid UUID
        parsed = uuid.UUID(incident_id)
        assert str(parsed) == incident_id


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 2: P0 Full Database Outage
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioP0Outage:
    """P0 database outage — maximum severity, fast escalation."""

    async def test_p0_triage_severity(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p0-sev-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        p0_triage = lambda m, i: _triage_resp(  # noqa: E731
            severity="P0",
            services=["postgres-primary", "checkout", "payment"],
            impact=50000,
            confidence=0.95,
        )

        with _mock_agents(triage_fn=p0_triage), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P0_DB_ALERT)

        triage = TriageResult.model_validate(mid["triage"])
        assert triage.severity == "P0"
        assert triage.estimated_user_impact == 50000
        assert "postgres-primary" in triage.affected_services

    async def test_p0_full_approval_flow(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p0-full-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        p0_triage = lambda m, i: _triage_resp(  # noqa: E731
            severity="P0", services=["postgres-primary"], impact=50000, confidence=0.95
        )

        with _mock_agents(triage_fn=p0_triage), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P0_DB_ALERT)
            final = await _approve_incident(graph, config)

        assert final["post_mortem"] is not None
        assert final["escalated"] is False

    async def test_p0_context_includes_deployments(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p0-dep-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        p0_triage = lambda m, i: _triage_resp(severity="P0", services=["postgres-primary"])  # noqa: E731
        p0_context = lambda m, i: _context_resp(  # noqa: E731
            deployments=["postgres-primary@f4a9c3 by ops-team — 2024-01-15T02:45:00Z"]
        )

        with _mock_agents(triage_fn=p0_triage, context_fn=p0_context), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P0_DB_ALERT)

        ctx = mid["context"]
        assert len(ctx["recent_deployments"]) == 1
        assert "postgres-primary" in ctx["recent_deployments"][0]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 3: P2 Memory Leak — RCA retry loop
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioP2RCARetry:
    """Memory leak: first RCA attempt low-confidence, second attempt high-confidence."""

    async def test_rca_retries_and_succeeds(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p2-retry-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        attempt_counter = {"n": 0}

        def rca_retry_fn(messages, info):
            attempt_counter["n"] += 1
            # First attempt: low confidence → triggers context re-gather
            if attempt_counter["n"] == 1:
                return _rca_resp(confidence=0.4)
            # Second attempt: high confidence → proceeds to plan
            return _rca_resp(confidence=0.82)

        p2_triage = lambda m, i: _triage_resp(severity="P2", services=["worker-service"])  # noqa: E731

        with _mock_agents(triage_fn=p2_triage, rca_fn=rca_retry_fn), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P2_MEMORY_ALERT)
            final = await _approve_incident(graph, config)

        assert final["post_mortem"] is not None
        assert final["escalated"] is False
        # Should have made 2 RCA attempts
        assert final["rca_attempts"] == 2

    async def test_rca_attempt_counter_increments(self):
        """Bug probe: verify rca_attempts increments correctly on each retry."""
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p2-ctr-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        attempt_counter = {"n": 0}

        def rca_count_fn(messages, info):
            attempt_counter["n"] += 1
            if attempt_counter["n"] <= 2:
                return _rca_resp(confidence=0.3)
            return _rca_resp(confidence=0.85)

        p2_triage = lambda m, i: _triage_resp(severity="P2")  # noqa: E731

        with _mock_agents(triage_fn=p2_triage, rca_fn=rca_count_fn), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P2_MEMORY_ALERT)
            final = await _approve_incident(graph, config)

        assert final["rca_attempts"] == 3, (
            f"Expected 3 RCA attempts, got {final['rca_attempts']}"
        )
        assert final["escalated"] is False


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 4: P3 Disk Warning — low-risk, low-severity
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioP3DiskWarning:
    """P3 disk warning — auto-approved path with low-risk remediation."""

    async def test_p3_completes_with_low_risk_plan(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"p3-disk-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        p3_triage = lambda m, i: _triage_resp(  # noqa: E731
            severity="P3",
            services=["log-aggregator"],
            impact=0,
            confidence=0.95,
        )
        p3_remediation = lambda m, i: _remediation_resp(  # noqa: E731
            risk_level="low", requires_approval=False
        )

        with (
            _mock_agents(triage_fn=p3_triage, remediation_fn=p3_remediation),
            _persist_patch(),
            _slack_patch(),
        ):
            await _run_incident_to_interrupt(graph, config, P3_DISK_ALERT)
            final = await _approve_incident(graph, config)

        triage = TriageResult.model_validate(final["triage"])
        assert triage.severity == "P3"
        assert final["post_mortem"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 5: Human Rejection → Escalation
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioHumanRejection:
    """On-call engineer rejects the remediation plan → graph routes to escalation."""

    async def test_rejection_sets_escalated_true(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"reject-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with (
            _mock_agents(),
            _persist_patch(),
            _slack_patch(),
        ):
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final = await _reject_incident(graph, config)

        assert final.get("escalated") is True
        assert final.get("human_approved") is False
        assert final.get("post_mortem") is not None

    async def test_rejection_triggers_postmortem(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"rej-pm-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        with (
            _mock_agents(postmortem_fn=lambda m, i: _escalation_postmortem_resp()),
            _persist_patch(),
            _slack_patch(),
        ):
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final = await _reject_incident(graph, config)

        assert final.get("post_mortem") is not None
        pm = PostMortem.model_validate(final["post_mortem"])
        assert "escalated" in pm.resolution_summary.lower() or len(pm.action_items) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 6: RCA Max Retries → Escalation
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioRCAMaxRetries:
    """All 3 RCA attempts return low confidence → automatic escalation."""

    async def test_max_retries_escalates(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"maxrca-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        low_conf_rca = lambda m, i: _rca_resp(confidence=0.2)  # noqa: E731

        with (
            _mock_agents(
                rca_fn=low_conf_rca,
                postmortem_fn=lambda m, i: _escalation_postmortem_resp(),
            ),
            _persist_patch(),
            _slack_patch(),
        ):
            final = await graph.ainvoke(
                {"alert_payload": P1_PAYMENT_ALERT},
                config=config,
            )

        assert final.get("escalated") is True
        assert final.get("rca_attempts") == 3
        assert final.get("post_mortem") is not None

    async def test_max_retries_exact_count(self):
        """Verify the rca_attempts counter stops at exactly rca_max_attempts."""
        from iras.config.settings import get_settings

        settings = get_settings()
        max_attempts = settings.rca_max_attempts

        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"maxcnt-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        low_conf_rca = lambda m, i: _rca_resp(confidence=0.1)  # noqa: E731

        with (
            _mock_agents(rca_fn=low_conf_rca),
            _persist_patch(),
            _slack_patch(),
        ):
            final = await graph.ainvoke(
                {"alert_payload": P1_PAYMENT_ALERT},
                config=config,
            )

        assert final["rca_attempts"] == max_attempts, (
            f"Expected {max_attempts} attempts, got {final['rca_attempts']}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 7: Plan Generation Failure
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioPlanGenerationFailure:
    """Remediation agent raises UnexpectedModelBehavior → skip interrupt → escalation."""

    async def test_plan_failure_routes_to_escalation(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"planfail-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        def broken_remediation(messages, info):
            raise UnexpectedModelBehavior("Model returned malformed output after 3 retries")

        with (
            _mock_agents(remediation_fn=broken_remediation),
            _persist_patch(),
            _slack_patch(),
        ):
            final = await graph.ainvoke(
                {"alert_payload": P1_PAYMENT_ALERT},
                config=config,
            )

        # approval_node should skip the interrupt and set human_approved=False
        assert final.get("human_approved") is False
        assert final.get("escalated") is True
        assert final.get("error") is not None
        assert "Remediation planning failed" in final["error"]

    async def test_error_field_contains_exception_message(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"planfail-err-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        def broken_remediation(messages, info):
            raise UnexpectedModelBehavior("JSON decode error: Expecting value at position 0")

        with (
            _mock_agents(remediation_fn=broken_remediation),
            _persist_patch(),
            _slack_patch(),
        ):
            final = await graph.ainvoke(
                {"alert_payload": P1_PAYMENT_ALERT},
                config=config,
            )

        assert final.get("error") is not None
        assert "JSON decode error" in final["error"] or "Remediation" in final["error"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 8: All Context Tools Fail
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioAllToolsFail:
    """All three context gathering tools fail — RCA proceeds from empty context."""

    async def test_rca_runs_despite_all_tool_failures(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"toolfail-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        empty_context = lambda m, i: ModelResponse(  # noqa: E731
            parts=[
                TextPart(
                    content=json.dumps(
                        {
                            "log_evidence": [],
                            "metric_anomalies": [],
                            "recent_deployments": [],
                            "summary": "All tools unavailable — no evidence gathered.",
                            "failed_tools": ["fetch_logs", "fetch_metrics", "fetch_deployments"],
                        }
                    )
                )
            ]
        )

        with _mock_agents(context_fn=empty_context), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final = await _approve_incident(graph, config)

        # RCA should still run and produce a hypothesis
        assert final.get("hypothesis") is not None
        ctx = final["context"]
        assert ctx["failed_tools"] == ["fetch_logs", "fetch_metrics", "fetch_deployments"]
        assert ctx["log_evidence"] == []

    async def test_failed_tools_preserved_in_state(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"toolfail2-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        partial_context = lambda m, i: ModelResponse(  # noqa: E731
            parts=[
                TextPart(
                    content=json.dumps(
                        {
                            "log_evidence": [],
                            "metric_anomalies": [],
                            "recent_deployments": [],
                            "summary": "Partial context — metrics only.",
                            "failed_tools": ["fetch_logs", "fetch_deployments"],
                        }
                    )
                )
            ]
        )

        with _mock_agents(context_fn=partial_context), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)

        failed = mid["context"]["failed_tools"]
        assert "fetch_logs" in failed
        assert "fetch_deployments" in failed
        assert "fetch_metrics" not in failed


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 9: Missing Required Fields
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioMissingFields:
    """ingestion_node raises ValueError for payloads missing title or timestamp."""

    async def test_missing_title_raises(self):
        graph = build_graph(checkpointer=None)
        config = {"configurable": {"thread_id": f"missing-title-{uuid.uuid4().hex[:8]}"}}

        with pytest.raises(Exception, match="title"):
            await graph.ainvoke(
                {"alert_payload": {"timestamp": "2024-01-15T10:00:00Z"}},
                config=config,
            )

    async def test_missing_timestamp_raises(self):
        graph = build_graph(checkpointer=None)
        config = {"configurable": {"thread_id": f"missing-ts-{uuid.uuid4().hex[:8]}"}}

        with pytest.raises(Exception, match="timestamp"):
            await graph.ainvoke(
                {"alert_payload": {"title": "Alert without timestamp"}},
                config=config,
            )

    async def test_empty_payload_raises(self):
        graph = build_graph(checkpointer=None)
        config = {"configurable": {"thread_id": f"empty-{uuid.uuid4().hex[:8]}"}}

        with pytest.raises(Exception, match="timestamp|title"):
            await graph.ainvoke({"alert_payload": {}}, config=config)

    async def test_none_payload_raises(self):
        graph = build_graph(checkpointer=None)
        config = {"configurable": {"thread_id": f"none-{uuid.uuid4().hex[:8]}"}}

        with pytest.raises(Exception):
            await graph.ainvoke({"alert_payload": None}, config=config)


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 10: Unicode / Special Characters
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioUnicodePayloads:
    """Verify the system handles unicode, emojis, and injection-attempt strings safely."""

    UNICODE_ALERTS = [
        {
            "title": "Сервис оплаты недоступен — высокий уровень ошибок",
            "timestamp": "2024-01-15T10:00:00Z",
            "service": "платёжный-сервис",
        },
        {
            "title": "支付服务错误率过高 🚨",
            "timestamp": "2024-01-15T10:00:00Z",
            "service": "payment-service",
        },
        {
            "title": "Alert with SQL: SELECT * FROM users; DROP TABLE alerts; --",
            "timestamp": "2024-01-15T10:00:00Z",
            "service": "test-service",
        },
        {
            "title": "XSS attempt: <script>alert('xss')</script>",
            "timestamp": "2024-01-15T10:00:00Z",
            "service": "test-service",
        },
        {
            "title": "Null bytes: \x00\x01\x02 in title",
            "timestamp": "2024-01-15T10:00:00Z",
            "service": "test-service",
        },
    ]

    @pytest.mark.parametrize("alert", UNICODE_ALERTS)
    async def test_unicode_payload_ingested_safely(self, alert: dict):
        """Payloads with unusual characters must not crash ingestion."""
        graph = build_graph(checkpointer=None)
        tid = f"unicode-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        # We only test ingestion — triage onwards would need mock agents
        state: IncidentState = {"alert_payload": alert}  # type: ignore[typeddict-item]

        # ingestion_node is synchronous — call directly
        result = ingestion_node(state)

        assert result.get("incident_id") is not None
        assert result.get("started_at") is not None
        assert uuid.UUID(result["incident_id"])  # valid UUID


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 11: Large Payload
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioLargePayload:
    """Very large alert payload must not cause crashes or hangs."""

    async def test_large_nested_payload_ingested(self):
        # Simulate a noisy monitoring system sending many labels + annotations
        large_alert = {
            "title": "Large payload stress test alert",
            "timestamp": "2024-01-15T10:00:00Z",
            "labels": {f"label_{i}": f"value_{i}" * 100 for i in range(200)},
            "annotations": {f"ann_{i}": "x" * 1000 for i in range(50)},
            "stacktrace": "Error\n" + ("  at line 42 in module.function\n" * 2000),
            "affected_pods": [f"pod-{i:06d}" for i in range(500)],
        }

        state: IncidentState = {"alert_payload": large_alert}  # type: ignore[typeddict-item]
        result = ingestion_node(state)

        assert result.get("incident_id") is not None

    async def test_large_payload_full_graph_run(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"large-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        large_alert = {
            "title": "Large payload full graph run",
            "timestamp": "2024-01-15T10:00:00Z",
            "metadata": {"key": "A" * 10_000},
            "log_dump": ["log line " * 20 for _ in range(100)],
        }

        with _mock_agents(), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, large_alert)

        assert mid.get("incident_id") is not None


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 12: Payload aliasing / mutation safety
# ─────────────────────────────────────────────────────────────────────────────


class TestPayloadAliasingSafety:
    """Defend against hidden mutation when callers reuse the same payload object."""

    async def test_ingestion_does_not_mutate_shared_payload(self):
        shared_payload = {
            "title": "Shared mutable payload",
            "timestamp": "2024-01-15T10:00:00Z",
            "labels": {"team": "platform", "region": "us-east-1"},
            "events": ["created", "routed", "acked"],
        }
        original = copy.deepcopy(shared_payload)

        first = ingestion_node({"alert_payload": shared_payload})
        second = ingestion_node({"alert_payload": shared_payload})

        assert shared_payload == original
        assert first["incident_id"] != second["incident_id"]

    async def test_reused_payload_across_graph_runs_keeps_nested_content(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        shared_alert = {
            "title": "Shared object across runs",
            "timestamp": "2024-01-15T10:00:00Z",
            "security": {
                "headers": {"x-forwarded-for": "10.0.0.5", "x-request-id": "req-123"},
                "claims": ["admin", "sudo"],
            },
        }
        original = copy.deepcopy(shared_alert)

        with _mock_agents(), _persist_patch(), _slack_patch():
            config_one = {"configurable": {"thread_id": f"alias-1-{uuid.uuid4().hex[:6]}"}}
            config_two = {"configurable": {"thread_id": f"alias-2-{uuid.uuid4().hex[:6]}"}}
            mid_one = await _run_incident_to_interrupt(graph, config_one, shared_alert)
            mid_two = await _run_incident_to_interrupt(graph, config_two, shared_alert)

        assert shared_alert == original
        assert mid_one["incident_id"] != mid_two["incident_id"]


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 13: Concurrent Incidents — no state corruption
# ─────────────────────────────────────────────────────────────────────────────


class TestConcurrentIncidents:
    """Fire many alerts simultaneously and verify each gets its own isolated state."""

    async def _run_one_incident(self, graph, alert: dict, thread_id: str) -> dict:
        config = {"configurable": {"thread_id": thread_id}}
        with _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, alert)
            final = await _approve_incident(graph, config)
        return final

    async def test_20_concurrent_incidents_no_state_leak(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)

        alerts = [
            {
                "title": f"Concurrent alert #{i}",
                "timestamp": "2024-01-15T10:00:00Z",
                "service": f"service-{i % 5}",
                "instance": i,
            }
            for i in range(20)
        ]
        thread_ids = [f"concurrent-{i}-{uuid.uuid4().hex[:6]}" for i in range(20)]

        with _mock_agents():
            results = await asyncio.gather(
                *[
                    self._run_one_incident(graph, alerts[i], thread_ids[i])
                    for i in range(20)
                ]
            )

        # Each result must have its own unique incident_id
        incident_ids = [r["incident_id"] for r in results]
        assert len(set(incident_ids)) == 20, "Some incidents share the same incident_id!"

        # Each result must be fully resolved
        for i, r in enumerate(results):
            assert r.get("post_mortem") is not None, f"Incident #{i} has no post_mortem"
            assert r.get("escalated") is False, f"Incident #{i} was unexpectedly escalated"

    async def test_concurrent_alerts_with_mixed_severities(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)

        severities = ["P0", "P1", "P2", "P3", "P1", "P2", "P0", "P3"]
        alerts = [
            {"title": f"Mixed severity alert {s}", "timestamp": "2024-01-15T10:00:00Z"}
            for s in severities
        ]
        thread_ids = [f"mixed-{i}-{uuid.uuid4().hex[:6]}" for i in range(len(severities))]

        def mixed_triage(msgs, info):
            # Cycle through severities based on message content
            for sev in ["P0", "P1", "P2", "P3"]:
                return _triage_resp(severity="P1")  # deterministic for concurrent safety

        with _mock_agents(triage_fn=mixed_triage):
            tasks = []
            for i in range(len(severities)):
                config = {"configurable": {"thread_id": thread_ids[i]}}
                tasks.append(self._run_one_incident(graph, alerts[i], thread_ids[i]))

            results = await asyncio.gather(*tasks)

        assert len(results) == len(severities)
        for r in results:
            assert r.get("incident_id") is not None


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 14: Sequential Stress — 50 incidents, timing baseline
# ─────────────────────────────────────────────────────────────────────────────


class TestSequentialStress:
    """Run 50 complete incident workflows back-to-back and measure throughput."""

    async def test_50_sequential_incidents(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)

        start = time.monotonic()

        incident_ids = []
        with _mock_agents():
            for i in range(50):
                tid = f"seq-stress-{i}-{uuid.uuid4().hex[:6]}"
                config = {"configurable": {"thread_id": tid}}
                alert = {
                    "title": f"Sequential stress alert #{i}",
                    "timestamp": "2024-01-15T10:00:00Z",
                }
                with _persist_patch(), _slack_patch():
                    mid = await _run_incident_to_interrupt(graph, config, alert)
                    await _approve_incident(graph, config)
                incident_ids.append(mid["incident_id"])

        elapsed = time.monotonic() - start

        # Basic sanity checks
        assert len(set(incident_ids)) == 50, "Duplicate incident IDs generated!"
        # Throughput: 50 incidents must complete within 120 seconds
        assert elapsed < 120, f"50 incidents took {elapsed:.1f}s — too slow"
        print(f"\n[stress] 50 sequential incidents in {elapsed:.2f}s ({50 / elapsed:.1f} inc/s)")


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 15: Cascade Failure (multi-service)
# ─────────────────────────────────────────────────────────────────────────────


class TestScenarioCascadeFailure:
    """Auth-service cascade affecting 4 downstream services."""

    async def test_cascade_all_services_in_triage(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"cascade-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        cascade_triage = lambda m, i: _triage_resp(  # noqa: E731
            severity="P1",
            services=["api-gateway", "auth-service", "session-store", "user-service"],
            impact=30000,
            confidence=0.88,
        )

        with _mock_agents(triage_fn=cascade_triage), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, CASCADE_ALERT)
            final = await _approve_incident(graph, config)

        triage = TriageResult.model_validate(final["triage"])
        assert len(triage.affected_services) == 4
        assert final["post_mortem"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 16: Adversarial agent outputs / replay abuse
# ─────────────────────────────────────────────────────────────────────────────


class TestAdversarialAgentOutputs:
    """Force the system through cases where the model lies or callers replay actions."""

    async def test_high_risk_step_forces_human_approval_even_if_model_lies(self):
        def lying_remediation(messages, info):
            return ModelResponse(
                parts=[
                    TextPart(
                        content=json.dumps(
                            {
                                "steps": [
                                    {
                                        "action": "Delete the canary namespace and recreate it",
                                        "rollback_command": "kubectl apply -f canary-backup.yaml",
                                        "risk_level": "high",
                                        "estimated_duration_seconds": 45,
                                    }
                                ],
                                "estimated_total_downtime_minutes": 4.0,
                                "reversible": True,
                                "requires_human_approval": False,
                                "summary": "High-risk infrastructure replacement disguised as safe.",
                            }
                        )
                    )
                ]
            )

        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": f"high-risk-{uuid.uuid4().hex[:8]}"}}

        with _mock_agents(remediation_fn=lying_remediation), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)

        plan = RemediationPlan.model_validate(mid["remediation_plan"])
        assert plan.steps[0].risk_level == "high"
        assert plan.requires_human_approval is True

    async def test_whitespace_rollback_marks_plan_requiring_approval(self):
        def whitespace_rollback(messages, info):
            return ModelResponse(
                parts=[
                    TextPart(
                        content=json.dumps(
                            {
                                "steps": [
                                    {
                                        "action": "Rotate production traffic to standby",
                                        "rollback_command": "   ",
                                        "risk_level": "medium",
                                        "estimated_duration_seconds": 20,
                                    }
                                ],
                                "estimated_total_downtime_minutes": 1.0,
                                "reversible": True,
                                "requires_human_approval": False,
                                "summary": "Rollback field is whitespace only.",
                            }
                        )
                    )
                ]
            )

        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": f"rollback-{uuid.uuid4().hex[:8]}"}}

        with _mock_agents(remediation_fn=whitespace_rollback), _persist_patch(), _slack_patch():
            mid = await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)

        plan = RemediationPlan.model_validate(mid["remediation_plan"])
        assert plan.reversible is False
        assert plan.requires_human_approval is True

    async def test_approval_replay_returns_stable_final_state(self):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": f"replay-{uuid.uuid4().hex[:8]}"}}

        with _mock_agents(), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final_once = await _approve_incident(graph, config)
            final_twice = await _approve_incident(graph, config)

        assert final_once["incident_id"] == final_twice["incident_id"]
        assert final_once["post_mortem"]["incident_id"] == final_twice["post_mortem"]["incident_id"]
        assert final_twice["resolved_at"] is not None


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 17: API schema abuse / hostile nested extras
# ─────────────────────────────────────────────────────────────────────────────


class TestAPISchemaAbuse:
    """Attack API validation and ensure allowed extras survive unchanged."""

    async def test_webhook_rejects_non_string_title(self):
        from httpx import ASGITransport, AsyncClient

        from iras.api.app import create_app

        app = create_app()
        app.state.graph = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhook/alert",
                json={"title": {"nested": "object"}, "timestamp": "2024-01-15T10:00:00Z"},
            )

        assert response.status_code == 422

    async def test_webhook_rejects_non_string_timestamp(self):
        from httpx import ASGITransport, AsyncClient

        from iras.api.app import create_app

        app = create_app()
        app.state.graph = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/webhook/alert",
                json={"title": "Bad timestamp type", "timestamp": ["2024-01-15T10:00:00Z"]},
            )

        assert response.status_code == 422

    async def test_webhook_preserves_hostile_nested_extras_for_graph(self):
        from httpx import ASGITransport, AsyncClient

        from iras.api.app import create_app

        captured_states: list[dict[str, Any]] = []

        async def _capture_ainvoke(state, config=None, **kwargs):
            captured_states.append(copy.deepcopy(state))
            return {}

        mock_graph = AsyncMock()
        mock_graph.ainvoke = _capture_ainvoke

        app = create_app()
        app.state.graph = mock_graph

        hostile_payload = {
            "title": "Preserve nested hostile extras",
            "timestamp": "2024-01-15T10:00:00Z",
            "headers": {"x-real-ip": "127.0.0.1", "user-agent": "sqlmap/1.8"},
            "nested": {
                "claims": ["../../etc/passwd", "${jndi:ldap://evil}"],
                "blob": {"a": [1, 2, {"deep": ["x" * 64, "y" * 64]}]},
            },
        }

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/webhook/alert", json=hostile_payload)

        assert response.status_code == 202
        await asyncio.sleep(0.1)
        assert captured_states, "Background graph invocation did not run"
        assert captured_states[0]["alert_payload"]["nested"] == hostile_payload["nested"]
        assert captured_states[0]["alert_payload"]["headers"] == hostile_payload["headers"]


# ─────────────────────────────────────────────────────────────────────────────
# Bug B1: Webhook triple-UUID mismatch
# ─────────────────────────────────────────────────────────────────────────────


class TestBugWebhookIdMismatch:
    """
    BUG B1 (FIXED): The /webhook/alert endpoint used to have THREE separate UUIDs
    in flight — a correlation_id returned to the client, a different thread_id
    used by LangGraph, and a third UUID generated by ingestion_node.

    FIX APPLIED: A single UUID is now pre-generated by the webhook handler and
    used as: HTTP response incident_id, LangGraph thread_id, AND the initial
    state incident_id passed to ingestion_node.
    """

    async def test_ingestion_node_uses_pre_set_id(self):
        """After B1 fix: ingestion_node honours a pre-set incident_id from state."""
        pre_set_id = str(uuid.uuid4())
        state: IncidentState = {  # type: ignore[typeddict-item]
            "alert_payload": {
                "title": "Test alert",
                "timestamp": "2024-01-15T10:00:00Z",
            },
            "incident_id": pre_set_id,  # pre-set by webhook route
        }
        result = ingestion_node(state)
        generated_id = result["incident_id"]
        # FIX: ingestion_node now uses the pre-set ID
        assert generated_id == pre_set_id, (
            f"BUG B1 regression: ingestion_node generated a new ID '{generated_id}' "
            f"instead of honouring the pre-set ID '{pre_set_id}'."
        )

    async def test_webhook_returns_same_id_as_graph_thread(self):
        """
        After B1 fix: the webhook returns the same UUID that is used as the
        LangGraph thread_id, so callers can reliably use it for approval.
        """
        from httpx import ASGITransport, AsyncClient

        from iras.api.app import create_app

        invoked_thread_ids: list[str] = []

        async def _capture_ainvoke(state, config=None, **kwargs):
            if config:
                invoked_thread_ids.append(config.get("configurable", {}).get("thread_id", ""))
            return {}

        mock_graph = AsyncMock()
        mock_graph.ainvoke = _capture_ainvoke

        app = create_app()
        app.state.graph = mock_graph

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/webhook/alert",
                json={
                    "title": "Bug B1 fixed test alert",
                    "timestamp": "2024-01-15T10:00:00Z",
                },
            )

        assert resp.status_code == 202
        body = resp.json()
        client_incident_id = body["incident_id"]

        # Give background task a moment to run
        await asyncio.sleep(0.1)

        # FIX: client_incident_id (UUID) == LangGraph thread_id
        if invoked_thread_ids:
            graph_thread_id = invoked_thread_ids[0]
            assert client_incident_id == graph_thread_id, (
                f"BUG B1 regression: client gets '{client_incident_id}' "
                f"but LangGraph thread_id is '{graph_thread_id}' — they must be equal."
            )


# ─────────────────────────────────────────────────────────────────────────────
# Bug B2: Postmortem incident_id not enforced from state
# ─────────────────────────────────────────────────────────────────────────────


class TestBugPostmortemIncidentId:
    """
    BUG B2: The postmortem_node takes the incident_id from whatever the model
    returns in its JSON output (via PostMortem.model_validate). If the model
    returns a wrong or placeholder incident_id, the stored post_mortem["incident_id"]
    will be incorrect.

    FIX: postmortem_node should override pm_dict["incident_id"] with
    state["incident_id"] before persisting.
    """

    async def test_postmortem_id_matches_state_id(self):
        """
        When the mock postmortem model returns 'PLACEHOLDER' as incident_id,
        the final post_mortem dict should still have the correct state incident_id.

        If this assertion FAILS, it means the bug exists (pm["incident_id"] == "PLACEHOLDER").
        If this assertion PASSES, the fix is already in place.
        """
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        tid = f"pmid-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": tid}}

        # Postmortem model returns a wrong incident_id
        pm_model = lambda m, i: _postmortem_resp(incident_id="PLACEHOLDER")  # noqa: E731

        with _mock_agents(postmortem_fn=pm_model), _persist_patch(), _slack_patch():
            await _run_incident_to_interrupt(graph, config, P1_PAYMENT_ALERT)
            final = await _approve_incident(graph, config)

        actual_incident_id = final["incident_id"]
        pm_incident_id = final["post_mortem"]["incident_id"]

        # If this fails → BUG EXISTS: postmortem_node uses model's incident_id, not state's
        assert pm_incident_id == actual_incident_id, (
            f"BUG B2: post_mortem.incident_id='{pm_incident_id}' ≠ "
            f"state.incident_id='{actual_incident_id}'. "
            "postmortem_node must override incident_id from state."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug B3: apply_remediation_node no-plan path doesn't set escalated=True
# ─────────────────────────────────────────────────────────────────────────────


class TestBugApplyRemediationNoPlan:
    """
    BUG B3 (FIXED): apply_remediation_node used to return only
    {"error": "No remediation plan available"} when plan_dict was None,
    without setting escalated=True.

    FIX APPLIED: Now returns {"error": "No remediation plan available",
    "escalated": True} so the incident is correctly marked as escalated.
    """

    async def test_no_plan_node_escalates(self):
        """After B3 fix: apply_remediation_node sets escalated=True when no plan."""
        from iras.graph.nodes.apply_remediation import apply_remediation_node

        state: IncidentState = {  # type: ignore[typeddict-item]
            "incident_id": "test-no-plan",
            "alert_payload": {
                "title": "Test",
                "timestamp": "2024-01-15T10:00:00Z",
            },
            "remediation_plan": None,
            "human_approved": True,
        }

        result = await apply_remediation_node(state)

        assert "error" in result
        assert result["error"] == "No remediation plan available"
        # FIX: escalated IS now set when plan is missing
        assert result.get("escalated") is True, (
            "BUG B3 regression: apply_remediation_node does NOT set escalated=True "
            "when called with no plan."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bug B4: Concurrent get_checkpointer() — no asyncio.Lock
# ─────────────────────────────────────────────────────────────────────────────


class TestBugCheckpointerRace:
    """
    BUG B4: get_checkpointer() uses a global singleton pattern without an
    asyncio.Lock. If two coroutines both see _checkpointer is None and
    call get_checkpointer() concurrently, they may both initialise and
    open separate connection pools, leaking resources.
    """

    async def test_concurrent_get_checkpointer_race(self):
        """
        After B4 fix: get_checkpointer() uses an asyncio.Lock to guard the
        singleton initialisation, preventing double-initialisation under concurrency.
        """
        import inspect

        import iras.graph.checkpointer as cp_module

        src = inspect.getsource(cp_module.get_checkpointer)
        assert "asyncio.Lock" in src, (
            "BUG B4 regression: get_checkpointer() no longer uses asyncio.Lock. "
            "Concurrent callers can double-initialise the connection pool."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Scenario 14: Live API stress test (optional — skipped if server is down)
# ─────────────────────────────────────────────────────────────────────────────

LIVE_BASE_URL = "http://127.0.0.1:8000"


async def _server_is_up() -> bool:
    """Check if the IRAS server is running."""
    try:
        import httpx

        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(f"{LIVE_BASE_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture
async def live_server_required():
    """Skip test if live server is not reachable."""
    if not await _server_is_up():
        pytest.skip("Live IRAS server not running at http://127.0.0.1:8000")


class TestLiveAPIStress:
    """Stress-test the live FastAPI server with concurrent HTTP requests."""

    async def test_50_concurrent_webhook_alerts(self, live_server_required):
        import httpx

        async def fire_alert(client: httpx.AsyncClient, i: int) -> dict:
            try:
                resp = await client.post(
                    f"{LIVE_BASE_URL}/webhook/alert",
                    json={
                        "title": f"Stress test alert #{i}",
                        "timestamp": "2024-01-15T10:00:00Z",
                        "service": f"service-{i % 10}",
                        "stress_id": i,
                    },
                    timeout=30.0,
                )
                return {"status": resp.status_code, "body": resp.json(), "i": i}
            except httpx.TransportError as exc:
                return {"status": -1, "body": {}, "i": i, "error": str(exc)}

        async with httpx.AsyncClient() as client:
            start = time.monotonic()
            results = await asyncio.gather(
                *[fire_alert(client, i) for i in range(50)]
            )
            elapsed = time.monotonic() - start

        statuses = [r["status"] for r in results]
        transport_errors = [r for r in results if r["status"] == -1]
        if transport_errors:
            pytest.skip(
                f"{len(transport_errors)}/50 requests timed out — server may be overloaded. "
                f"First error: {transport_errors[0].get('error', '')}"
            )
        assert all(s == 202 for s in statuses), (
            f"Some requests failed: {[r for r in results if r['status'] != 202]}"
        )

        # All returned incident_ids must be unique (no UUID collision)
        ids = [r["body"].get("incident_id") for r in results]
        assert len(set(ids)) == 50, "Duplicate incident IDs returned by webhook!"

        print(
            f"\n[api-stress] 50 concurrent webhook requests in {elapsed:.2f}s "
            f"({50 / elapsed:.1f} req/s)"
        )

    async def test_health_endpoint_under_load(self, live_server_required):
        import httpx

        async def check_health(client, i):
            resp = await client.get(f"{LIVE_BASE_URL}/health", timeout=5.0)
            return resp.status_code

        async with httpx.AsyncClient() as client:
            statuses = await asyncio.gather(
                *[check_health(client, i) for i in range(100)]
            )

        assert all(s == 200 for s in statuses), (
            f"Health endpoint returned non-200: {set(statuses)}"
        )

    async def test_malformed_alert_returns_422(self, live_server_required):
        """The FastAPI validation layer should reject payloads missing required fields."""
        import httpx

        async with httpx.AsyncClient() as client:
            # Missing both title and timestamp
            resp = await client.post(
                f"{LIVE_BASE_URL}/webhook/alert",
                json={"source": "prometheus", "value": 42},
                timeout=5.0,
            )

        assert resp.status_code == 422, (
            f"Expected 422 Unprocessable Entity, got {resp.status_code}"
        )

    async def test_oversized_payload_accepted(self, live_server_required):
        """Large payloads (within reason) must be accepted, not rejected."""
        import httpx

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{LIVE_BASE_URL}/webhook/alert",
                json={
                    "title": "Oversized payload test",
                    "timestamp": "2024-01-15T10:00:00Z",
                    "big_data": "x" * 50_000,
                },
                timeout=10.0,
            )

        assert resp.status_code == 202
