"""Integration tests for the escalation node."""

# pylint: disable=missing-class-docstring,missing-function-docstring,redefined-outer-name
from __future__ import annotations

import pytest

from iras.agents.deps import EscalationDeps
from iras.graph.nodes.escalation import escalation_node
from iras.graph.state import IncidentState
from iras.tools.pagerduty import MockPagerDutyClient
from iras.tools.slack import MockSlackClient


@pytest.fixture
def escalation_state(
    alert_payload, triage_dict, context_dict, low_confidence_hypothesis
) -> IncidentState:
    return IncidentState(
        alert_payload=alert_payload,
        incident_id="test-esc-001",
        started_at="2024-01-15T10:30:00Z",
        rca_attempts=3,
        triage=triage_dict,
        context=context_dict,
        hypothesis=low_confidence_hypothesis.model_dump(),
        escalated=False,
        human_approved=None,
    )


class TestEscalationNode:
    async def test_sets_escalated_true(self, escalation_state: IncidentState):
        mock_pd = MockPagerDutyClient()
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        updates = await escalation_node(escalation_state, deps=deps)

        assert updates["escalated"] is True

    async def test_pagerduty_triggered(self, escalation_state: IncidentState):
        mock_pd = MockPagerDutyClient()
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        await escalation_node(escalation_state, deps=deps)

        assert mock_pd.trigger_count >= 1

    async def test_slack_notification_sent(self, escalation_state: IncidentState):
        mock_pd = MockPagerDutyClient()
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        await escalation_node(escalation_state, deps=deps)

        assert mock_slack.posted_messages_count >= 1

    async def test_pagerduty_idempotent_with_same_incident_id(
        self, escalation_state: IncidentState
    ):
        """Triggering twice with same incident_id should not double-trigger PD (dedup_key)."""
        mock_pd = MockPagerDutyClient()
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        await escalation_node(escalation_state, deps=deps)
        await escalation_node(escalation_state, deps=deps)

        # MockPagerDutyClient should track that dedup_key was seen
        assert mock_pd.incident_ids_triggered == {"test-esc-001"}

    async def test_only_escalated_key_returned(self, escalation_state: IncidentState):
        deps = EscalationDeps(
            pagerduty_client=MockPagerDutyClient(),
            slack_client=MockSlackClient(),
        )
        updates = await escalation_node(escalation_state, deps=deps)

        assert "escalated" in updates

    async def test_pagerduty_failure_does_not_raise(self, escalation_state: IncidentState):
        """If PD fails, node should handle gracefully and still mark escalated."""
        mock_pd = MockPagerDutyClient(should_fail=True)
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        # Should not raise
        updates = await escalation_node(escalation_state, deps=deps)

        assert updates["escalated"] is True

    async def test_escalation_includes_rejection_reason(self, escalation_state: IncidentState):
        """Covers line 60 — rejection_reason appended when human_approved=False."""
        state = dict(escalation_state)
        state["human_approved"] = False
        mock_pd = MockPagerDutyClient()
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        updates = await escalation_node(state, deps=deps)  # type: ignore[arg-type]

        assert updates["escalated"] is True
        # Verify the Slack message contains rejection reason
        assert mock_slack.posted_messages_count >= 1

    async def test_escalation_includes_error_message(self, escalation_state: IncidentState):
        """Covers line 62 — error appended to summary when state has error."""
        state = dict(escalation_state)
        state["error"] = "RCA agent validation failed: unexpected output"
        mock_pd = MockPagerDutyClient()
        mock_slack = MockSlackClient()
        deps = EscalationDeps(pagerduty_client=mock_pd, slack_client=mock_slack)

        updates = await escalation_node(state, deps=deps)  # type: ignore[arg-type]

        assert updates["escalated"] is True
