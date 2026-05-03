"""Integration tests for approval_node and route_after_approval."""

# pylint: disable=missing-class-docstring,missing-function-docstring,redefined-outer-name
from __future__ import annotations

from unittest.mock import patch

import pytest
from langgraph.errors import NodeInterrupt

from iras.graph.nodes.approval import approval_node, route_after_approval
from iras.graph.state import IncidentState


@pytest.fixture
def approval_state(base_incident_state, triage_dict, plan_dict) -> IncidentState:
    state = dict(base_incident_state)
    state["triage"] = triage_dict
    state["remediation_plan"] = plan_dict
    state["error"] = None
    return state  # type: ignore[return-value]


class TestApprovalNode:
    def test_no_plan_error_skips_interrupt(self, base_incident_state):
        """When plan generation failed, approval is skipped → human_approved=False."""
        state = dict(base_incident_state)
        state["error"] = "Remediation planning failed"
        state["remediation_plan"] = None

        updates = approval_node(state)
        assert updates["human_approved"] is False

    def test_plan_present_raises_node_interrupt(self, approval_state):
        """When plan exists, interrupt() should be called, raising NodeInterrupt."""
        with patch(
            "iras.graph.nodes.approval.interrupt", side_effect=NodeInterrupt("approval_required")
        ):
            with pytest.raises(NodeInterrupt):
                approval_node(approval_state)

    def test_interrupt_payload_contains_incident_id(self, approval_state):
        """The interrupt payload should contain incident_id."""
        interrupt_value = None

        def fake_interrupt(value):
            nonlocal interrupt_value
            interrupt_value = value
            raise NodeInterrupt(value)

        with patch("iras.graph.nodes.approval.interrupt", side_effect=fake_interrupt):
            with pytest.raises(NodeInterrupt):
                approval_node(approval_state)

        assert interrupt_value["incident_id"] == approval_state["incident_id"]  # pylint: disable=unsubscriptable-object
        assert interrupt_value["type"] == "approval_required"  # pylint: disable=unsubscriptable-object

    def test_resume_with_approved_true(self, approval_state):
        """When interrupt() returns approved=True, human_approved should be True."""
        with patch("iras.graph.nodes.approval.interrupt", return_value={"approved": True}):
            updates = approval_node(approval_state)
        assert updates["human_approved"] is True

    def test_resume_with_approved_false(self, approval_state):
        """When interrupt() returns approved=False, human_approved should be False."""
        with patch("iras.graph.nodes.approval.interrupt", return_value={"approved": False}):
            updates = approval_node(approval_state)
        assert updates["human_approved"] is False

    def test_resume_with_empty_dict_defaults_to_false(self, approval_state):
        """Missing 'approved' key defaults to False."""
        with patch("iras.graph.nodes.approval.interrupt", return_value={}):
            updates = approval_node(approval_state)
        assert updates["human_approved"] is False


class TestRouteAfterApproval:
    def test_approved_routes_to_apply_remediation(self, base_incident_state):
        state = dict(base_incident_state)
        state["human_approved"] = True
        assert route_after_approval(state) == "apply_remediation"

    def test_rejected_routes_to_escalation(self, base_incident_state):
        state = dict(base_incident_state)
        state["human_approved"] = False
        assert route_after_approval(state) == "escalation"

    def test_none_routes_to_escalation(self, base_incident_state):
        state = dict(base_incident_state)
        state["human_approved"] = None
        assert route_after_approval(state) == "escalation"
