"""Unit tests for the post-mortem agent."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from iras.agents.deps import PostMortemDeps
from iras.agents.postmortem import postmortem_agent, run_postmortem
from iras.models.incident import PostMortem


def _make_postmortem_response(incident_id: str = "test-001") -> ModelResponse:
    payload = {
        "incident_id": incident_id,
        "timeline": [
            "10:30 UTC — Alert fired",
            "10:31 UTC — Triage complete",
            "10:40 UTC — Remediation applied",
            "10:46 UTC — Resolved",
        ],
        "root_cause_summary": "DB connection pool exhaustion.",
        "resolution_summary": "Increased pool size and restarted service.",
        "action_items": [
            "Add automated pool scaling",
            "Add pool exhaustion alert",
            "Review pool settings across all services",
        ],
        "severity": "P1",
        "total_duration_minutes": 16,
    }
    return ModelResponse(parts=[TextPart(content=json.dumps(payload))])


class TestPostMortemAgent:
    async def test_happy_path(self, base_incident_state: dict[str, Any]):
        def model(messages, info):
            return _make_postmortem_response(incident_id="test-incident-001")

        with postmortem_agent.override(model=FunctionModel(model)):
            result = await run_postmortem(dict(base_incident_state), deps=PostMortemDeps())

        assert isinstance(result, PostMortem)
        assert result.incident_id == "test-incident-001"
        assert len(result.timeline) >= 3
        assert len(result.action_items) >= 1
        assert result.total_duration_minutes > 0

    async def test_action_items_present(self, base_incident_state: dict[str, Any]):
        def model(messages, info):
            return _make_postmortem_response()

        with postmortem_agent.override(model=FunctionModel(model)):
            result = await run_postmortem(dict(base_incident_state), deps=PostMortemDeps())

        assert len(result.action_items) >= 1
        for item in result.action_items:
            assert isinstance(item, str)
            assert len(item) > 0

    async def test_severity_carried_through(self, base_incident_state: dict[str, Any]):
        def model(messages, info):
            return _make_postmortem_response()

        with postmortem_agent.override(model=FunctionModel(model)):
            result = await run_postmortem(dict(base_incident_state), deps=PostMortemDeps())

        assert result.severity in ("P0", "P1", "P2", "P3")

    async def test_model_dump_round_trip(self, base_incident_state: dict[str, Any]):
        def model(messages, info):
            return _make_postmortem_response()

        with postmortem_agent.override(model=FunctionModel(model)):
            result = await run_postmortem(dict(base_incident_state), deps=PostMortemDeps())

        restored = PostMortem.model_validate(result.model_dump())
        assert restored.incident_id == result.incident_id
        assert restored.action_items == result.action_items

    async def test_none_deps_uses_default(self, base_incident_state: dict[str, Any]):
        def model(messages, info):
            return _make_postmortem_response()

        with postmortem_agent.override(model=FunctionModel(model)):
            result = await run_postmortem(dict(base_incident_state), deps=None)

        assert result is not None

    async def test_escalated_incident_postmortem(self):
        """Post-mortem should work when incident was escalated (no remediation plan)."""
        escalated_state = {
            "incident_id": "esc-001",
            "started_at": "2024-01-15T10:30:00Z",
            "alert_payload": {"title": "DB outage", "timestamp": "2024-01-15T10:30:00Z"},
            "triage": {
                "severity": "P0",
                "affected_services": ["postgres"],
                "estimated_user_impact": 100000,
                "confidence": 0.95,
                "reasoning": "Complete DB failure",
            },
            "hypothesis": None,
            "remediation_plan": None,
            "escalated": True,
            "rca_attempts": 3,
        }

        def model(messages, info):
            payload = {
                "incident_id": "esc-001",
                "timeline": ["10:30 UTC — Alert", "10:45 UTC — Escalated to on-call"],
                "root_cause_summary": "Unknown — insufficient evidence after 3 RCA attempts.",
                "resolution_summary": "Escalated to on-call DBA for manual investigation.",
                "action_items": ["Improve DB monitoring"],
                "severity": "P0",
                "total_duration_minutes": 15,
            }
            return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

        with postmortem_agent.override(model=FunctionModel(model)):
            result = await run_postmortem(escalated_state, deps=PostMortemDeps())

        assert result.severity == "P0"
        assert result.incident_id == "esc-001"

    async def test_retry_exhausted_raises(self, base_incident_state: dict[str, Any]):
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        def bad_model(messages, info):
            return ModelResponse(parts=[TextPart(content="NOT JSON")])

        with postmortem_agent.override(model=FunctionModel(bad_model)):
            with pytest.raises(UnexpectedModelBehavior):
                await run_postmortem(dict(base_incident_state), deps=PostMortemDeps())
