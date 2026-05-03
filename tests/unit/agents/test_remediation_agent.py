"""Unit tests for the remediation planning agent."""

# pylint: disable=missing-class-docstring,missing-function-docstring,unused-argument,import-outside-toplevel
from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import RemediationDeps
from iras.agents.remediation import remediation_agent, run_remediation_planning
from iras.models.incident import RemediationPlan


def _make_remediation_response(
    risk_levels: list[str] | None = None,
    reversible: bool = True,
) -> ModelResponse:
    steps = []
    for i, risk in enumerate(risk_levels or ["low", "medium"]):
        steps.append(
            {
                "action": f"Step {i + 1}: remediation action",
                "rollback_command": f"kubectl rollout undo deployment/service-{i}",
                "risk_level": risk,
                "estimated_duration_seconds": 30 * (i + 1),
            }
        )
    payload = {
        "steps": steps,
        "estimated_total_downtime_minutes": 5,
        "reversible": reversible,
        "requires_human_approval": any(s["risk_level"] == "high" for s in steps),
        "summary": "Remediation plan summary",
    }
    return ModelResponse(parts=[TextPart(content=json.dumps(payload))])


def low_risk_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_remediation_response(risk_levels=["low"])


def high_risk_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_remediation_response(risk_levels=["low", "high"])


class TestRemediationAgent:
    async def test_happy_path_low_risk(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
        hypothesis_dict: dict[str, Any],
    ):
        with remediation_agent.override(model=FunctionModel(low_risk_model)):
            result = await run_remediation_planning(
                alert_payload, triage_dict, context_dict, hypothesis_dict, deps=RemediationDeps()
            )

        assert isinstance(result, RemediationPlan)
        assert len(result.steps) >= 1
        assert result.steps[0].risk_level == "low"
        assert result.requires_human_approval is False

    async def test_high_risk_forces_approval(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
        hypothesis_dict: dict[str, Any],
    ):
        with remediation_agent.override(model=FunctionModel(high_risk_model)):
            result = await run_remediation_planning(
                alert_payload, triage_dict, context_dict, hypothesis_dict, deps=RemediationDeps()
            )

        # model_validator should have forced requires_human_approval=True for high-risk
        assert result.requires_human_approval is True

    async def test_all_steps_have_rollback(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
        hypothesis_dict: dict[str, Any],
    ):
        with remediation_agent.override(model=FunctionModel(low_risk_model)):
            result = await run_remediation_planning(
                alert_payload, triage_dict, context_dict, hypothesis_dict, deps=RemediationDeps()
            )

        for step in result.steps:
            assert step.rollback_command, "Every step must have a rollback command"

    async def test_model_dump_round_trip(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
        hypothesis_dict: dict[str, Any],
    ):
        with remediation_agent.override(model=FunctionModel(low_risk_model)):
            result = await run_remediation_planning(
                alert_payload, triage_dict, context_dict, hypothesis_dict, deps=RemediationDeps()
            )

        restored = RemediationPlan.model_validate(result.model_dump())
        assert len(restored.steps) == len(result.steps)
        assert restored.summary == result.summary

    async def test_none_deps_uses_default(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
        hypothesis_dict: dict[str, Any],
    ):
        with remediation_agent.override(model=FunctionModel(low_risk_model)):
            result = await run_remediation_planning(
                alert_payload, triage_dict, context_dict, hypothesis_dict, deps=None
            )

        assert result is not None

    async def test_retry_exhausted_raises(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_dict: dict[str, Any],
        hypothesis_dict: dict[str, Any],
    ):
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        def bad_model(messages, info):
            return ModelResponse(parts=[TextPart(content="BAD")])

        with remediation_agent.override(model=FunctionModel(bad_model)):
            with pytest.raises(UnexpectedModelBehavior):
                await run_remediation_planning(
                    alert_payload,
                    triage_dict,
                    context_dict,
                    hypothesis_dict,
                    deps=RemediationDeps(),
                )
