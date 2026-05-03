"""Unit tests for the RCA agent."""

# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,import-outside-toplevel
from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import RCADeps
from iras.agents.rca import rca_agent, run_rca
from iras.models.incident import RootCauseHypothesis


def _make_rca_response(confidence: float = 0.88) -> ModelResponse:
    payload = {
        "primary_cause": "DB connection pool exhaustion after deployment",
        "contributing_factors": ["pool size not scaled", "missing health check"],
        "evidence": [
            {
                "timestamp": "2024-01-15T10:29:55Z",
                "log_line": "ERROR: connection refused to postgres-primary",
                "relevance_score": 0.95,
            }
        ],
        "confidence": confidence,
        "recommended_action": "Increase connection pool size and restart service",
        "alternative_hypotheses": ["Network partition"],
    }
    return ModelResponse(parts=[TextPart(content=json.dumps(payload))])


def high_confidence_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_rca_response(confidence=0.88)


def low_confidence_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_rca_response(confidence=0.35)


def boundary_confidence_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_rca_response(confidence=0.7)


class TestRCAAgent:
    async def test_high_confidence_rca(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        with rca_agent.override(model=FunctionModel(high_confidence_model)):
            result = await run_rca(alert_payload, triage_dict, context_dict, deps=RCADeps())

        assert isinstance(result, RootCauseHypothesis)
        assert result.confidence >= 0.7
        assert result.primary_cause

    async def test_low_confidence_rca(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        with rca_agent.override(model=FunctionModel(low_confidence_model)):
            result = await run_rca(alert_payload, triage_dict, context_dict, deps=RCADeps())

        assert result.confidence < 0.7

    async def test_confidence_boundary(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        """Confidence == threshold (0.7) should pass."""
        with rca_agent.override(model=FunctionModel(boundary_confidence_model)):
            result = await run_rca(alert_payload, triage_dict, context_dict, deps=RCADeps())

        assert result.confidence == 0.7

    async def test_empty_contributing_factors_valid(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        def model(messages, info):
            payload = {
                "primary_cause": "Unknown root cause",
                "contributing_factors": [],
                "evidence": [],
                "confidence": 0.5,
                "recommended_action": "Escalate",
                "alternative_hypotheses": [],
            }
            return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

        with rca_agent.override(model=FunctionModel(model)):
            result = await run_rca(alert_payload, triage_dict, context_dict, deps=RCADeps())

        assert result.contributing_factors == []

    async def test_model_dump_round_trip(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        with rca_agent.override(model=FunctionModel(high_confidence_model)):
            result = await run_rca(alert_payload, triage_dict, context_dict, deps=RCADeps())

        restored = RootCauseHypothesis.model_validate(result.model_dump())
        assert restored.confidence == result.confidence
        assert restored.primary_cause == result.primary_cause

    async def test_none_deps_uses_default(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        with rca_agent.override(model=FunctionModel(high_confidence_model)):
            result = await run_rca(alert_payload, triage_dict, context_dict, deps=None)

        assert result is not None

    async def test_retry_exhausted_raises(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
    ):
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        call_count = 0

        def bad_model(messages, info):
            nonlocal call_count
            call_count += 1
            return ModelResponse(parts=[TextPart(content="NOT JSON")])

        with rca_agent.override(model=FunctionModel(bad_model)):
            with pytest.raises(UnexpectedModelBehavior):
                await run_rca(alert_payload, triage_dict, context_dict, deps=RCADeps())

        assert call_count >= 2
