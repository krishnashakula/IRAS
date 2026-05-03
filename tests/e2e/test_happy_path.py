"""E2E tests — happy path (triage → RCA → plan → approve → remediate → postmortem)."""

# pylint: disable=unused-argument,missing-class-docstring,missing-function-docstring,import-outside-toplevel
from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.context_gathering import context_agent
from iras.agents.postmortem import postmortem_agent
from iras.agents.rca import rca_agent
from iras.agents.remediation import remediation_agent
from iras.agents.triage import triage_agent
from iras.graph.builder import build_graph
from iras.models.incident import PostMortem

# ── FunctionModel factories ────────────────────────────────────────────────────


def _triage_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "severity": "P1",
                        "affected_services": ["payment-service"],
                        "estimated_user_impact": 5000,
                        "confidence": 0.9,
                        "reasoning": "High error rate on payment-service.",
                    }
                )
            )
        ]
    )


def _context_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
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
                        "recent_deployments": ["payment-service@abc123"],
                        "summary": "DB connectivity issues.",
                        "failed_tools": [],
                    }
                )
            )
        ]
    )


def _rca_model_high_confidence(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "primary_cause": "DB connection pool exhaustion",
                        "contributing_factors": ["pool size not scaled"],
                        "evidence": [
                            {
                                "timestamp": "2024-01-15T10:29:55Z",
                                "log_line": "ERROR: connection refused",
                                "relevance_score": 0.95,
                            }
                        ],
                        "confidence": 0.88,
                        "recommended_action": "Increase pool size",
                        "alternative_hypotheses": [],
                    }
                )
            )
        ]
    )


def _remediation_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "steps": [
                            {
                                "action": "Increase DB connection pool from 10 to 50",
                                "rollback_command": (
                                    "kubectl set env deployment/app DB_POOL_SIZE=10"
                                ),
                                "risk_level": "low",
                                "estimated_duration_seconds": 30,
                            }
                        ],
                        "estimated_total_downtime_minutes": 2,
                        "reversible": True,
                        "requires_human_approval": False,
                        "summary": "Increase pool size and restart service.",
                    }
                )
            )
        ]
    )


def _postmortem_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    # incident_id will be set dynamically — we use a placeholder; the agent fills it from state
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "incident_id": "dynamic",  # node should override with actual id
                        "timeline": [
                            "10:30 UTC — Alert",
                            "10:31 UTC — Triage",
                            "10:40 UTC — Remediation applied",
                        ],
                        "root_cause_summary": "DB connection pool exhaustion.",
                        "resolution_summary": "Increased pool size.",
                        "action_items": ["Add automated pool scaling"],
                        "severity": "P1",
                        "total_duration_minutes": 16,
                    }
                )
            )
        ]
    )


@contextlib.contextmanager
def _all_agents_mocked():
    """Context manager that overrides all five agents with FunctionModels."""
    with (
        triage_agent.override(model=FunctionModel(_triage_model)),
        context_agent.override(model=FunctionModel(_context_model)),
        rca_agent.override(model=FunctionModel(_rca_model_high_confidence)),
        remediation_agent.override(model=FunctionModel(_remediation_model)),
        postmortem_agent.override(model=FunctionModel(_postmortem_model)),
    ):
        yield


class TestHappyPath:
    async def test_full_graph_completes_after_approval(self, alert_payload: dict[str, Any]):
        """Graph runs to completion when human approves the remediation plan."""
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-001"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                # First run — should pause at approval interrupt
                await graph.ainvoke({"alert_payload": alert_payload}, config=config)

                # Resume with approval
                final_state = await graph.ainvoke(Command(resume={"approved": True}), config=config)

        assert final_state.get("post_mortem") is not None

    async def test_post_mortem_is_valid(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-002"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                final_state = await graph.ainvoke(Command(resume={"approved": True}), config=config)

        pm = PostMortem.model_validate(final_state["post_mortem"])
        assert pm.severity in ("P0", "P1", "P2", "P3")
        assert len(pm.action_items) >= 1
        assert pm.total_duration_minutes > 0

    async def test_resolved_at_set(self, alert_payload: dict[str, Any]):
        import re

        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-003"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                final_state = await graph.ainvoke(Command(resume={"approved": True}), config=config)

        assert final_state.get("resolved_at") is not None
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", final_state["resolved_at"])

    async def test_incident_not_escalated_on_happy_path(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-004"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                final_state = await graph.ainvoke(Command(resume={"approved": True}), config=config)

        assert final_state.get("escalated") is False

    async def test_human_approved_is_true(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-005"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                final_state = await graph.ainvoke(Command(resume={"approved": True}), config=config)

        assert final_state.get("human_approved") is True

    async def test_intermediate_state_has_triage_and_hypothesis(
        self, alert_payload: dict[str, Any]
    ):
        """State after first run (at interrupt) should already have triage + hypothesis."""
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-006"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                first_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        assert first_state.get("triage") is not None
        assert first_state.get("hypothesis") is not None
        assert first_state.get("remediation_plan") is not None

    async def test_p0_alert_happy_path(self, p0_alert_payload: dict[str, Any]):
        """P0 alert should also complete the happy path."""
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "happy-007"}}

        with _all_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                await graph.ainvoke({"alert_payload": p0_alert_payload}, config=config)
                final_state = await graph.ainvoke(Command(resume={"approved": True}), config=config)

        assert final_state.get("post_mortem") is not None
