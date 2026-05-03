"""E2E tests — crash recovery via Postgres checkpointing.
# pylint: disable=redefined-outer-name

These tests require a live PostgreSQL database.  They are skipped automatically
unless the ``POSTGRES_URL`` environment variable is set.

Run manually with:
    POSTGRES_URL=postgresql://user:pass@localhost:5432/iras_test pytest -m requires_postgres
"""

from __future__ import annotations

import contextlib
import json
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.types import Command
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.context_gathering import context_agent
from iras.agents.postmortem import postmortem_agent
from iras.agents.rca import rca_agent
from iras.agents.remediation import remediation_agent
from iras.agents.triage import triage_agent
from iras.graph.builder import build_graph
from iras.graph.checkpointer import close_checkpointer, get_checkpointer

pytestmark = pytest.mark.requires_postgres

# Skip all tests in this module unless POSTGRES_URL is set
postgres_url = os.environ.get("POSTGRES_URL", "")
if not postgres_url:
    pytest.skip(
        "Skipping crash-recovery tests — set POSTGRES_URL to run them",
        allow_module_level=True,
    )


# ── FunctionModel factories (same as happy path) ──────────────────────────────


def _triage_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
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


def _context_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "log_evidence": [
                            {
                                "timestamp": "2024-01-15T10:29:55Z",
                                "log_line": "ERROR: connection refused",
                                "relevance_score": 0.95,
                            }
                        ],
                        "metric_anomalies": [],
                        "recent_deployments": [],
                        "summary": "DB connectivity issues.",
                        "failed_tools": [],
                    }
                )
            )
        ]
    )


def _rca_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "primary_cause": "DB connection pool exhaustion",
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


def _remediation_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
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
                        "summary": "Increase pool size.",
                    }
                )
            )
        ]
    )


def _postmortem_model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    return ModelResponse(
        parts=[
            TextPart(
                content=json.dumps(
                    {
                        "incident_id": "crash-test",
                        "timeline": ["10:30 UTC — Alert", "10:46 UTC — Resolved"],
                        "root_cause_summary": "DB pool exhaustion.",
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
    with (
        triage_agent.override(model=FunctionModel(_triage_model)),
        context_agent.override(model=FunctionModel(_context_model)),
        rca_agent.override(model=FunctionModel(_rca_model)),
        remediation_agent.override(model=FunctionModel(_remediation_model)),
        postmortem_agent.override(model=FunctionModel(_postmortem_model)),
    ):
        yield


class TestCrashRecovery:
    """E2E tests verifying graph state is checkpointed and can be resumed after a crash."""

    async def test_state_persisted_at_interrupt(self, alert_payload: dict[str, Any]):
        """After the first run (at interrupt), state is persisted to Postgres."""
        checkpointer = await get_checkpointer(postgres_url)
        graph = build_graph(checkpointer=checkpointer)
        thread_id = "crash-persist-001"
        config = {"configurable": {"thread_id": thread_id}}

        try:
            with _all_agents_mocked():
                with patch(
                    "iras.graph.nodes.postmortem._persist_postmortem",
                    new_callable=AsyncMock,
                ):
                    await graph.ainvoke({"alert_payload": alert_payload}, config=config)

            # Verify state was persisted
            # aget_tuple() returns Optional[CheckpointTuple];
            # checkpoint field holds channel_values
            saved = await checkpointer.aget_tuple(config)
            assert saved is not None
            assert saved.checkpoint["channel_values"].get("triage") is not None
        finally:
            await close_checkpointer()

    async def test_resume_from_new_graph_instance(self, alert_payload: dict[str, Any]):
        """Simulates a crash: first graph instance is abandoned; a new instance
        reads from Postgres checkpointer and resumes from the interrupt point."""
        thread_id = "crash-resume-001"
        config = {"configurable": {"thread_id": thread_id}}

        # First graph instance — runs until interrupt, then "crashes"
        checkpointer1 = await get_checkpointer(postgres_url)
        graph1 = build_graph(checkpointer=checkpointer1)

        try:
            with _all_agents_mocked():
                with patch(
                    "iras.graph.nodes.postmortem._persist_postmortem",
                    new_callable=AsyncMock,
                ):
                    await graph1.ainvoke({"alert_payload": alert_payload}, config=config)
        finally:
            await close_checkpointer()

        # Second graph instance — "new process after crash" — reads from same Postgres
        checkpointer2 = await get_checkpointer(postgres_url)
        graph2 = build_graph(checkpointer=checkpointer2)

        try:
            with _all_agents_mocked():
                with patch(
                    "iras.graph.nodes.postmortem._persist_postmortem",
                    new_callable=AsyncMock,
                ):
                    final_state = await graph2.ainvoke(
                        Command(resume={"approved": True}), config=config
                    )
        finally:
            await close_checkpointer()

        assert final_state.get("post_mortem") is not None
        assert final_state.get("resolved_at") is not None

    async def test_multiple_resumes_idempotent(self, alert_payload: dict[str, Any]):
        """Resuming an already-completed thread should return the final state."""
        checkpointer = await get_checkpointer(postgres_url)
        graph = build_graph(checkpointer=checkpointer)
        thread_id = "crash-idem-001"
        config = {"configurable": {"thread_id": thread_id}}

        try:
            with _all_agents_mocked():
                with patch(
                    "iras.graph.nodes.postmortem._persist_postmortem",
                    new_callable=AsyncMock,
                ):
                    await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                    await graph.ainvoke(Command(resume={"approved": True}), config=config)

            # A second resume of a completed thread should return the same state
            second_resume = await graph.ainvoke(Command(resume={"approved": True}), config=config)
            assert second_resume.get("post_mortem") is not None
        finally:
            await close_checkpointer()

    async def test_rejection_persisted_and_escalation_runs(self, alert_payload: dict[str, Any]):
        """Reject decision must be persisted; graph then routes to escalation."""
        checkpointer = await get_checkpointer(postgres_url)
        graph = build_graph(checkpointer=checkpointer)
        thread_id = "crash-reject-001"
        config = {"configurable": {"thread_id": thread_id}}

        try:
            with _all_agents_mocked():
                with patch(
                    "iras.graph.nodes.postmortem._persist_postmortem",
                    new_callable=AsyncMock,
                ):
                    await graph.ainvoke({"alert_payload": alert_payload}, config=config)
                    final_state = await graph.ainvoke(
                        Command(resume={"approved": False}), config=config
                    )

            assert final_state.get("escalated") is True
            assert final_state.get("human_approved") is False
        finally:
            await close_checkpointer()
