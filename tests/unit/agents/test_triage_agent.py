"""Unit tests for the triage agent.

Uses FunctionModel to avoid real LLM calls.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import TriageDeps
from iras.agents.triage import run_triage, triage_agent
from iras.models.incident import TriageResult


def _make_triage_response(
    severity: str = "P1",
    affected_services: list[str] | None = None,
    estimated_user_impact: int = 5000,
    confidence: float = 0.9,
    reasoning: str = "High error rate detected.",
):
    """Return a FunctionModel message list that produces a TriageResult."""
    payload = {
        "severity": severity,
        "affected_services": affected_services or ["payment-service"],
        "estimated_user_impact": estimated_user_impact,
        "confidence": confidence,
        "reasoning": reasoning,
    }
    return [ModelResponse(parts=[TextPart(content=json.dumps(payload))])]


def happy_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_triage_response()[0]


def p0_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_triage_response(
        severity="P0", affected_services=["postgres-primary"], confidence=0.95
    )[0]


def low_confidence_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_triage_response(severity="P3", estimated_user_impact=10, confidence=0.55)[0]


class TestTriageAgent:
    async def test_happy_path_p1(self, alert_payload: dict[str, Any]):
        with triage_agent.override(model=FunctionModel(happy_model)):
            result = await run_triage(alert_payload, deps=TriageDeps())

        assert isinstance(result, TriageResult)
        assert result.severity == "P1"
        assert result.confidence == 0.9
        assert "payment-service" in result.affected_services

    async def test_happy_path_p0(self, p0_alert_payload: dict[str, Any]):
        with triage_agent.override(model=FunctionModel(p0_model)):
            result = await run_triage(p0_alert_payload, deps=TriageDeps())

        assert result.severity == "P0"
        assert result.confidence >= 0.9

    async def test_low_confidence_valid(self, alert_payload: dict[str, Any]):
        with triage_agent.override(model=FunctionModel(low_confidence_model)):
            result = await run_triage(alert_payload, deps=TriageDeps())

        assert result.severity == "P3"
        assert 0.0 <= result.confidence <= 1.0

    async def test_none_deps_uses_default(self, alert_payload: dict[str, Any]):
        """run_triage should accept deps=None and use default TriageDeps."""
        with triage_agent.override(model=FunctionModel(happy_model)):
            result = await run_triage(alert_payload, deps=None)

        assert result is not None

    async def test_model_dump_round_trip(self, alert_payload: dict[str, Any]):
        with triage_agent.override(model=FunctionModel(happy_model)):
            result = await run_triage(alert_payload, deps=TriageDeps())

        restored = TriageResult.model_validate(result.model_dump())
        assert restored.severity == result.severity
        assert restored.confidence == result.confidence

    async def test_all_severities_valid(self, alert_payload: dict[str, Any]):
        for severity in ("P0", "P1", "P2", "P3"):

            def make_model(s=severity):
                def m(messages, info):
                    return _make_triage_response(severity=s)[0]

                return m

            with triage_agent.override(model=FunctionModel(make_model())):
                result = await run_triage(alert_payload, deps=TriageDeps())

            assert result.severity == severity

    async def test_retry_exhausted_raises(self, alert_payload: dict[str, Any]):
        """If FunctionModel always returns invalid JSON, agent raises after retries."""
        call_count = 0

        def bad_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal call_count
            call_count += 1
            return ModelResponse(parts=[TextPart(content="NOT JSON")])

        from pydantic_ai.exceptions import UnexpectedModelBehavior

        with triage_agent.override(model=FunctionModel(bad_model)):
            with pytest.raises(UnexpectedModelBehavior):
                await run_triage(alert_payload, deps=TriageDeps())

        assert call_count >= 2  # at least one retry occurred
