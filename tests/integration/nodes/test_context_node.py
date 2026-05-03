"""Integration tests for the context-gathering node."""

# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,redefined-outer-name,reimported,import-outside-toplevel
from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.context_gathering import context_agent
from iras.agents.deps import ContextDeps
from iras.graph.nodes.context_gathering import context_gathering_node
from iras.graph.state import IncidentState
from iras.models.incident import ContextBundle


def make_context_fn(failed_tools: list[str] | None = None):
    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        payload = {
            "log_evidence": [
                {
                    "timestamp": "2024-01-15T10:29:55Z",
                    "log_line": "ERROR: connection refused",
                    "relevance_score": 0.92,
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
            "failed_tools": failed_tools or [],
        }
        return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

    return fn


@pytest.fixture
def context_state(alert_payload, triage_dict) -> IncidentState:
    return IncidentState(
        alert_payload=alert_payload,
        incident_id="test-001",
        started_at="2024-01-15T10:30:00Z",
        rca_attempts=0,
        triage=triage_dict,
    )


class TestContextGatheringNode:
    async def test_writes_context_to_state(
        self, context_state: IncidentState, context_deps: ContextDeps
    ):
        with context_agent.override(model=FunctionModel(make_context_fn())):
            updates = await context_gathering_node(context_state, deps=context_deps)

        assert "context" in updates
        assert updates["context"] is not None

    async def test_context_is_valid_bundle(
        self, context_state: IncidentState, context_deps: ContextDeps
    ):
        with context_agent.override(model=FunctionModel(make_context_fn())):
            updates = await context_gathering_node(context_state, deps=context_deps)

        bundle = ContextBundle.model_validate(updates["context"])
        assert len(bundle.log_evidence) >= 1

    async def test_only_context_key_returned(
        self, context_state: IncidentState, context_deps: ContextDeps
    ):
        with context_agent.override(model=FunctionModel(make_context_fn())):
            updates = await context_gathering_node(context_state, deps=context_deps)

        assert set(updates.keys()) == {"context"}

    async def test_failed_tools_tracked(
        self, context_state: IncidentState, context_deps: ContextDeps
    ):
        with context_agent.override(
            model=FunctionModel(make_context_fn(failed_tools=["fetch_metrics"]))
        ):
            updates = await context_gathering_node(context_state, deps=context_deps)

        bundle = ContextBundle.model_validate(updates["context"])
        assert "fetch_metrics" in bundle.failed_tools

    async def test_json_serializable(self, context_state: IncidentState, context_deps: ContextDeps):
        import json as j

        with context_agent.override(model=FunctionModel(make_context_fn())):
            updates = await context_gathering_node(context_state, deps=context_deps)

        j.dumps(updates["context"])  # should not raise

    async def test_default_deps_created_when_none(self, alert_payload, triage_dict):
        """Node should not crash when deps=None."""
        state = IncidentState(
            alert_payload=alert_payload,
            incident_id="test-001",
            started_at="2024-01-15T10:30:00Z",
            rca_attempts=0,
            triage=triage_dict,
        )
        with context_agent.override(model=FunctionModel(make_context_fn())):
            updates = await context_gathering_node(state, deps=None)

        assert "context" in updates
