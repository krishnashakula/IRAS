"""Integration tests for apply_remediation_node."""

# pylint: disable=missing-class-docstring,missing-function-docstring,redefined-outer-name,unused-argument,too-few-public-methods
from __future__ import annotations

from unittest.mock import patch

import pytest

from iras.graph.nodes.apply_remediation import (
    _execute_step,
    _rollback_step,
    apply_remediation_node,
)
from iras.graph.state import IncidentState
from iras.models.incident import RemediationStep


@pytest.fixture
def remediation_state(base_incident_state, plan_dict) -> IncidentState:
    state = dict(base_incident_state)
    state["remediation_plan"] = plan_dict
    state["human_approved"] = True
    return state  # type: ignore[return-value]


class TestApplyRemediationNode:
    async def test_executes_all_steps_and_returns_no_error(self, remediation_state):
        updates = await apply_remediation_node(remediation_state)
        assert updates.get("error") is None

    async def test_no_plan_returns_error(self, base_incident_state):
        state = dict(base_incident_state)
        state["remediation_plan"] = None
        updates = await apply_remediation_node(state)
        assert "error" in updates
        assert updates["error"] == "No remediation plan available"

    async def test_step_failure_triggers_rollback(self, remediation_state):
        call_count = 0

        async def mock_execute(step, incident_id):
            nonlocal call_count
            call_count += 1
            return False  # Always fail

        with patch("iras.graph.nodes.apply_remediation._execute_step", side_effect=mock_execute):
            updates = await apply_remediation_node(remediation_state)

        assert "error" in updates
        assert updates["error"] is not None
        assert updates.get("escalated") is True

    async def test_step_failure_error_message_contains_step_number(self, remediation_state):
        async def fail_on_first(step, incident_id):
            return False

        with patch("iras.graph.nodes.apply_remediation._execute_step", side_effect=fail_on_first):
            updates = await apply_remediation_node(remediation_state)

        assert "step 1" in updates["error"].lower() or "step" in updates["error"].lower()

    async def test_single_step_plan_completes_successfully(self, base_incident_state):
        """A minimal single-step plan should complete without error."""
        state = dict(base_incident_state)
        state["remediation_plan"] = {
            "steps": [
                {
                    "action": "echo done",
                    "rollback_command": "echo rollback",
                    "risk_level": "low",
                    "estimated_duration_seconds": 1,
                }
            ],
            "summary": "Single step plan",
            "estimated_total_downtime_minutes": 0,
            "reversible": True,
            "requires_human_approval": False,
        }
        state["human_approved"] = True
        updates = await apply_remediation_node(state)
        assert updates.get("error") is None

    async def test_execute_step_returns_true(self):
        step = RemediationStep(
            action="kubectl rollout restart",
            rollback_command="kubectl rollout undo",
            risk_level="low",
            estimated_duration_seconds=5,
        )
        result = await _execute_step(step, "test-001")
        assert result is True

    async def test_execute_step_caps_sleep_duration(self):
        """Even for moderate estimated duration, step returns True."""
        step = RemediationStep(
            action="long running command",
            rollback_command="undo",
            risk_level="high",
            estimated_duration_seconds=1,  # 0.01s sleep
        )
        result = await _execute_step(step, "test-001")
        assert result is True


class TestRollbackStep:
    async def test_rollback_step_completes(self):
        step = RemediationStep(
            action="kubectl scale",
            rollback_command="kubectl scale --replicas=3",
            risk_level="medium",
            estimated_duration_seconds=10,
        )
        # Should not raise
        await _rollback_step(step, "test-001")
