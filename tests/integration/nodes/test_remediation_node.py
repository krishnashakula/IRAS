"""Integration tests for the generate_plan node."""

# pylint: disable=redefined-outer-name,reimported,import-outside-toplevel
from __future__ import annotations

import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import ApprovalDeps, RemediationDeps
from iras.agents.remediation import remediation_agent
from iras.graph.nodes.generate_plan import generate_plan_node
from iras.graph.state import IncidentState
from iras.models.incident import RemediationPlan
from iras.tools.slack import MockSlackClient


def make_plan_fn():
    """Return a FunctionModel callback that produces a canned RemediationPlan response."""

    def fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        payload = {
            "steps": [
                {
                    "action": "Increase DB connection pool size",
                    "rollback_command": "kubectl set env deployment/app DB_POOL_SIZE=10",
                    "risk_level": "low",
                    "estimated_duration_seconds": 30,
                }
            ],
            "estimated_total_downtime_minutes": 2,
            "reversible": True,
            "requires_human_approval": False,
            "summary": "Increase pool size and restart service.",
        }
        return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

    return fn


@pytest.fixture
def plan_state(alert_payload, triage_dict, context_dict, hypothesis_dict) -> IncidentState:
    """Return an IncidentState populated with alert, triage, context, and hypothesis data."""
    return IncidentState(
        alert_payload=alert_payload,
        incident_id="test-001",
        started_at="2024-01-15T10:30:00Z",
        rca_attempts=1,
        triage=triage_dict,
        context=context_dict,
        hypothesis=hypothesis_dict,
        escalated=False,
    )


class TestGeneratePlanNode:
    """Integration tests for the generate_plan_node graph node."""

    async def test_writes_remediation_plan(self, plan_state: IncidentState):
        """Node must populate the remediation_plan key in its output dict."""
        mock_slack = MockSlackClient()
        with remediation_agent.override(model=FunctionModel(make_plan_fn())):
            updates = await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=mock_slack),
                remediation_deps=RemediationDeps(),
            )

        assert "remediation_plan" in updates
        assert updates["remediation_plan"] is not None

    async def test_slack_approval_sent(self, plan_state: IncidentState):
        """Node must post at least one Slack approval message."""
        mock_slack = MockSlackClient()
        with remediation_agent.override(model=FunctionModel(make_plan_fn())):
            await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=mock_slack),
                remediation_deps=RemediationDeps(),
            )

        assert mock_slack.posted_messages_count >= 1

    async def test_thread_ts_saved_in_state(self, plan_state: IncidentState):
        """Node must save the Slack thread timestamp to approval_slack_thread_ts."""
        mock_slack = MockSlackClient()
        with remediation_agent.override(model=FunctionModel(make_plan_fn())):
            updates = await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=mock_slack),
                remediation_deps=RemediationDeps(),
            )

        assert updates.get("approval_slack_thread_ts") is not None

    async def test_plan_is_valid_remediation_plan(self, plan_state: IncidentState):
        """The remediation_plan dict must be parseable as a RemediationPlan pydantic model."""
        mock_slack = MockSlackClient()
        with remediation_agent.override(model=FunctionModel(make_plan_fn())):
            updates = await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=mock_slack),
                remediation_deps=RemediationDeps(),
            )

        plan = RemediationPlan.model_validate(updates["remediation_plan"])
        assert len(plan.steps) >= 1

    async def test_json_serializable(self, plan_state: IncidentState):
        """The remediation_plan output must be JSON-serialisable without errors."""
        import json as j

        mock_slack = MockSlackClient()
        with remediation_agent.override(model=FunctionModel(make_plan_fn())):
            updates = await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=mock_slack),
                remediation_deps=RemediationDeps(),
            )

        j.dumps(updates["remediation_plan"])

    async def test_unexpected_model_behavior_returns_error(self, plan_state: IncidentState):
        """Covers lines 65-71 — UnexpectedModelBehavior sets error and null plan."""
        from unittest.mock import patch

        from pydantic_ai.exceptions import UnexpectedModelBehavior

        with patch(
            "iras.graph.nodes.generate_plan.run_remediation_planning",
            side_effect=UnexpectedModelBehavior("model failed to produce plan"),
        ):
            updates = await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=MockSlackClient()),
                remediation_deps=RemediationDeps(),
            )

        assert updates["remediation_plan"] is None
        assert "Remediation planning failed" in updates["error"]

    async def test_slack_failure_returns_none_thread_ts(self, plan_state: IncidentState):
        """Covers line 115 — Slack notification failure logs warning and sets ts=None."""
        from iras.tools.slack import MockSlackClient as _MockSlack

        failing_slack = _MockSlack(should_fail=True)
        with remediation_agent.override(model=FunctionModel(make_plan_fn())):
            updates = await generate_plan_node(
                plan_state,
                approval_deps=ApprovalDeps(slack_client=failing_slack),
                remediation_deps=RemediationDeps(),
            )

        assert updates["approval_slack_thread_ts"] is None
        assert updates["remediation_plan"] is not None
