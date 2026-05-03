"""Integration tests for the RCA node."""

# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,redefined-outer-name,reimported,import-outside-toplevel,too-few-public-methods
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import RCADeps
from iras.agents.rca import rca_agent
from iras.graph.nodes.rca import rca_node, route_after_rca
from iras.graph.state import IncidentState


def _make_mock_settings(*, threshold: float = 0.7, max_attempts: int = 3) -> MagicMock:
    m = MagicMock()
    m.rca_confidence_threshold = threshold
    m.rca_max_attempts = max_attempts
    return m


def make_rca_fn(confidence: float = 0.88):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        payload = {
            "primary_cause": "DB connection pool exhaustion",
            "contributing_factors": ["pool size not scaled"],
            "evidence": [
                {
                    "timestamp": "2024-01-15T10:29:55Z",
                    "log_line": "ERROR: connection refused",
                    "relevance_score": 0.95,
                }
            ],
            "confidence": confidence,
            "recommended_action": "Increase pool size",
            "alternative_hypotheses": [],
        }
        return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

    return fn


@pytest.fixture
def rca_state(alert_payload, triage_dict, context_dict) -> IncidentState:
    return IncidentState(
        alert_payload=alert_payload,
        incident_id="test-001",
        started_at="2024-01-15T10:30:00Z",
        rca_attempts=0,
        triage=triage_dict,
        context=context_dict,
        hypothesis=None,
        escalated=False,
    )


class TestRCANode:
    async def test_writes_hypothesis_to_state(self, rca_state: IncidentState):
        with rca_agent.override(model=FunctionModel(make_rca_fn(confidence=0.88))):
            updates = await rca_node(rca_state, deps=RCADeps())

        assert "hypothesis" in updates
        assert updates["hypothesis"] is not None

    async def test_increments_rca_attempts(self, rca_state: IncidentState):
        with rca_agent.override(model=FunctionModel(make_rca_fn())):
            updates = await rca_node(rca_state, deps=RCADeps())

        assert updates["rca_attempts"] == 1

    async def test_increments_from_existing_attempts(self, rca_state: IncidentState):
        rca_state["rca_attempts"] = 2
        with rca_agent.override(model=FunctionModel(make_rca_fn())):
            updates = await rca_node(rca_state, deps=RCADeps())

        assert updates["rca_attempts"] == 3

    async def test_json_serializable(self, rca_state: IncidentState):
        import json as j

        with rca_agent.override(model=FunctionModel(make_rca_fn())):
            updates = await rca_node(rca_state, deps=RCADeps())

        j.dumps(updates["hypothesis"])


class TestRouteAfterRCA:
    @pytest.fixture(autouse=True)
    def mock_get_settings(self):
        with patch("iras.graph.nodes.rca.get_settings", return_value=_make_mock_settings()):
            yield

    def test_high_confidence_routes_to_generate_plan(self, rca_state: IncidentState):
        rca_state["hypothesis"] = {
            "primary_cause": "DB pool exhaustion",
            "contributing_factors": [],
            "evidence": [],
            "confidence": 0.88,
            "recommended_action": "Restart",
            "alternative_hypotheses": [],
        }
        rca_state["rca_attempts"] = 1

        route = route_after_rca(rca_state)
        assert route == "generate_plan"

    def test_low_confidence_retries_context(self, rca_state: IncidentState):
        rca_state["hypothesis"] = {
            "primary_cause": "Unknown",
            "contributing_factors": [],
            "evidence": [],
            "confidence": 0.35,
            "recommended_action": "Escalate",
            "alternative_hypotheses": [],
        }
        rca_state["rca_attempts"] = 1

        route = route_after_rca(rca_state)
        assert route == "context_gathering"

    def test_max_attempts_routes_to_escalation(self, rca_state: IncidentState):
        rca_state["hypothesis"] = {
            "primary_cause": "Unknown",
            "contributing_factors": [],
            "evidence": [],
            "confidence": 0.35,
            "recommended_action": "Escalate",
            "alternative_hypotheses": [],
        }
        rca_state["rca_attempts"] = 3  # >= max_attempts (3)

        route = route_after_rca(rca_state)
        assert route == "escalation"

    def test_boundary_confidence_routes_to_generate_plan(self, rca_state: IncidentState):
        rca_state["hypothesis"] = {
            "primary_cause": "Known",
            "contributing_factors": [],
            "evidence": [],
            "confidence": 0.7,  # exactly at threshold
            "recommended_action": "Fix it",
            "alternative_hypotheses": [],
        }
        rca_state["rca_attempts"] = 1

        route = route_after_rca(rca_state)
        assert route == "generate_plan"

    def test_route_escalation_when_error_and_no_hypothesis(self, rca_state: IncidentState):
        """Covers line 122 — escalation route when error is set and hypothesis is None."""
        rca_state["error"] = "RCA agent validation failed: unexpected response"
        rca_state["hypothesis"] = None
        rca_state["rca_attempts"] = 1

        route = route_after_rca(rca_state)
        assert route == "escalation"


class TestRCANodeUnexpectedModelBehavior:
    async def test_rca_node_unexpected_model_behavior_routes_to_escalation(
        self, rca_state: IncidentState
    ):
        """Covers lines 88-101 — UnexpectedModelBehavior forces escalation."""
        from unittest.mock import patch

        from pydantic_ai.exceptions import UnexpectedModelBehavior

        with patch(
            "iras.graph.nodes.rca.run_rca",
            side_effect=UnexpectedModelBehavior("model returned invalid JSON"),
        ):
            with patch("iras.graph.nodes.rca.get_settings") as mock_settings:
                mock_settings.return_value.rca_max_attempts = 3
                updates = await rca_node(rca_state, deps=RCADeps())

        assert updates["hypothesis"] is None
        assert updates["rca_attempts"] == 3
        assert "RCA agent validation failed" in updates["error"]
