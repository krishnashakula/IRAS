"""Integration tests for the triage node."""

# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,reimported,import-outside-toplevel
from __future__ import annotations

import json
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import TriageDeps
from iras.agents.triage import triage_agent
from iras.graph.nodes.triage import triage_node
from iras.graph.state import IncidentState
from iras.models.incident import TriageResult


def make_triage_fn(severity: str = "P1", confidence: float = 0.9):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        payload = {
            "severity": severity,
            "affected_services": ["payment-service"],
            "estimated_user_impact": 5000,
            "confidence": confidence,
            "reasoning": "High error rate detected.",
        }
        return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

    return fn


class TestTriageNode:
    async def test_writes_triage_to_state(self, alert_payload: dict[str, Any]):
        state = IncidentState(
            alert_payload=alert_payload,
            incident_id="test-001",
            started_at="2024-01-15T10:30:00Z",
            rca_attempts=0,
        )
        with triage_agent.override(model=FunctionModel(make_triage_fn())):
            updates = await triage_node(state, deps=TriageDeps())

        assert "triage" in updates
        assert updates["triage"] is not None

    async def test_triage_result_is_valid(self, alert_payload: dict[str, Any]):
        state = IncidentState(
            alert_payload=alert_payload,
            incident_id="test-001",
            started_at="2024-01-15T10:30:00Z",
            rca_attempts=0,
        )
        with triage_agent.override(
            model=FunctionModel(make_triage_fn(severity="P0", confidence=0.95))
        ):
            updates = await triage_node(state, deps=TriageDeps())

        result = TriageResult.model_validate(updates["triage"])
        assert result.severity == "P0"
        assert result.confidence == 0.95

    async def test_triage_dict_is_json_serializable(self, alert_payload: dict[str, Any]):
        import json as json_module

        state = IncidentState(
            alert_payload=alert_payload,
            incident_id="test-001",
            started_at="2024-01-15T10:30:00Z",
            rca_attempts=0,
        )
        with triage_agent.override(model=FunctionModel(make_triage_fn())):
            updates = await triage_node(state, deps=TriageDeps())

        # Should not raise
        json_module.dumps(updates["triage"])

    async def test_only_triage_key_returned(self, alert_payload: dict[str, Any]):
        """Node should only return {triage: ...} update dict."""
        state = IncidentState(
            alert_payload=alert_payload,
            incident_id="test-001",
            started_at="2024-01-15T10:30:00Z",
            rca_attempts=0,
        )
        with triage_agent.override(model=FunctionModel(make_triage_fn())):
            updates = await triage_node(state, deps=TriageDeps())

        assert set(updates.keys()) == {"triage"}
