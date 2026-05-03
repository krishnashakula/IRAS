"""E2E tests — escalation path (RCA never reaches confidence threshold)."""

# pylint: disable=unused-argument,missing-class-docstring,missing-function-docstring
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
                        "reasoning": "High error rate.",
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
                        "log_evidence": [],
                        "metric_anomalies": [],
                        "recent_deployments": [],
                        "summary": "Insufficient evidence.",
                        "failed_tools": [],
                    }
                )
            )
        ]
    )


def _rca_low_confidence_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Always returns low confidence — triggers retry loop → escalation."""
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "primary_cause": "Unknown root cause — insufficient data.",
                        "contributing_factors": [],
                        "evidence": [],
                        "confidence": 0.3,
                        "recommended_action": "Manual investigation required.",
                        "alternative_hypotheses": [],
                    }
                )
            )
        ]
    )


def _postmortem_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "incident_id": "esc-test",
                        "timeline": [
                            "10:30 UTC — Alert",
                            "10:45 UTC — Escalated after 3 failed RCA attempts",
                        ],
                        "root_cause_summary": "Unknown — escalated after max RCA attempts.",
                        "resolution_summary": (
                            "Escalated to on-call engineer for manual investigation."
                        ),
                        "action_items": ["Improve observability", "Add more log context"],
                        "severity": "P1",
                        "total_duration_minutes": 20,
                    }
                )
            )
        ]
    )


@contextlib.contextmanager
def _escalation_agents_mocked():
    """Override all agents — RCA always low confidence."""
    with (
        triage_agent.override(model=FunctionModel(_triage_model)),
        context_agent.override(model=FunctionModel(_context_model)),
        rca_agent.override(model=FunctionModel(_rca_low_confidence_model)),
        postmortem_agent.override(model=FunctionModel(_postmortem_model)),
    ):
        yield


class TestEscalationPath:
    async def test_escalation_after_max_rca_attempts(self, alert_payload: dict[str, Any]):
        """Low-confidence RCA repeated 3 times → escalation → postmortem."""
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-001"}}

        with _escalation_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                final_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        # No interrupt should occur — graph completes without human approval
        assert final_state.get("escalated") is True

    async def test_no_human_approval_needed_on_escalation(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-002"}}

        with _escalation_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                final_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        # human_approved should remain None (not set) since escalation bypasses approval
        assert final_state.get("human_approved") is None

    async def test_post_mortem_generated_after_escalation(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-003"}}

        with _escalation_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                final_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        assert final_state.get("post_mortem") is not None

    async def test_rca_attempts_equals_max(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-004"}}

        with _escalation_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                final_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        assert final_state.get("rca_attempts") >= 3

    async def test_post_mortem_valid_on_escalation(self, alert_payload: dict[str, Any]):
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-005"}}

        with _escalation_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                final_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        pm = PostMortem.model_validate(final_state["post_mortem"])
        assert len(pm.action_items) >= 1

    async def test_rejection_also_escalates(self, alert_payload: dict[str, Any]):
        """Human rejecting a valid plan should route to escalation."""
        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-006"}}

        def _rca_high_confidence(messages, info):
            return ModelResponse(
                parts=[
                    TextPart(
                        content=json.dumps(
                            {
                                "primary_cause": "DB pool exhaustion",
                                "contributing_factors": [],
                                "evidence": [],
                                "confidence": 0.88,
                                "recommended_action": "Increase pool size",
                                "alternative_hypotheses": [],
                            }
                        )
                    )
                ]
            )

        def _plan_model(messages, info):
            return ModelResponse(
                parts=[
                    TextPart(
                        content=json.dumps(
                            {
                                "steps": [
                                    {
                                        "action": "Increase pool",
                                        "rollback_command": "undo",
                                        "risk_level": "low",
                                        "estimated_duration_seconds": 30,
                                    }
                                ],
                                "estimated_total_downtime_minutes": 2,
                                "reversible": True,
                                "requires_human_approval": False,
                                "summary": "Increase pool.",
                            }
                        )
                    )
                ]
            )

        with (
            triage_agent.override(model=FunctionModel(_triage_model)),
            context_agent.override(model=FunctionModel(_context_model)),
            rca_agent.override(model=FunctionModel(_rca_high_confidence)),
            remediation_agent.override(model=FunctionModel(_plan_model)),
            postmortem_agent.override(model=FunctionModel(_postmortem_model)),
        ):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                # First run — pauses at approval
                await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                # Reject the plan → should escalate
                final_state = await graph.ainvoke(
                    Command(resume={"approved": False}), config=config
                )

        assert final_state.get("escalated") is True
        assert final_state.get("human_approved") is False

    async def test_resolved_at_set_on_escalation_path(self, alert_payload: dict[str, Any]):

        checkpointer = MemorySaver()
        graph = build_graph(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": "esc-007"}}

        with _escalation_agents_mocked():
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                final_state = await graph.ainvoke({"alert_payload": alert_payload}, config=config)

        assert final_state.get("resolved_at") is not None
