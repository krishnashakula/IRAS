"""Node 7 — Manual Escalation.

Executed when:
  a) RCA confidence cannot reach the threshold after max_attempts
  b) The human rejects the remediation plan
  c) The approval timeout fires

Creates a PagerDuty incident (idempotent — safe to call multiple times with
the same incident_id) and sends a structured Slack message with all available
context. Routes unconditionally to the post-mortem node.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from iras.agents.deps import EscalationDeps
from iras.graph.state import IncidentState
from iras.tools.slack import SlackMessage

logger = logging.getLogger(__name__)


async def escalation_node(
    state: IncidentState,
    *,
    deps: EscalationDeps | None = None,
) -> dict[str, Any]:
    """Page the on-call engineer via PagerDuty and post a Slack summary.

    Args:
        state: Current IncidentState.
        deps: EscalationDeps injection (PagerDuty + Slack clients).

    Returns:
        State update dict with ``escalated`` set to True.
    """
    incident_id = state.get("incident_id", "unknown")
    _deps = deps or EscalationDeps()
    triage = state.get("triage") or {}
    hypothesis = state.get("hypothesis") or {}
    context = state.get("context") or {}

    severity = triage.get("severity", "P1")
    affected_services = triage.get("affected_services", [])
    primary_cause = hypothesis.get("primary_cause", "Root cause undetermined")
    rca_attempts = state.get("rca_attempts", 0)
    rejection_reason = (
        "Human rejected remediation plan" if state.get("human_approved") is False else None
    )
    error = state.get("error")

    summary_lines = [
        f"[{severity}] IRAS escalated incident {incident_id} — manual intervention required.",
        f"Affected services: {', '.join(affected_services) or 'unknown'}",
        f"Root cause analysis: {primary_cause}",
        f"RCA attempts: {rca_attempts}",
    ]
    if rejection_reason:
        summary_lines.append(f"Reason for escalation: {rejection_reason}")
    if error:
        summary_lines.append(f"System error: {error}")

    summary = "\n".join(summary_lines)

    logger.warning(
        "Escalating incident to on-call",
        extra={
            "incident_id": incident_id,
            "node_name": "escalation",
            "timestamp": "now",
            "severity": severity,
            "rca_attempts": rca_attempts,
        },
    )

    # ── PagerDuty ─────────────────────────────────────────────────────────────
    pd_result = await _deps.pagerduty_client.trigger_incident(
        incident_id=incident_id,
        severity=severity,
        summary=f"[IRAS] {severity} incident requires manual intervention: {incident_id}",
        details={
            "incident_id": incident_id,
            "affected_services": affected_services,
            "primary_cause": primary_cause,
            "rca_attempts": rca_attempts,
            "triage": triage,
            "context_summary": context.get("summary", ""),
            "hypothesis": hypothesis,
            "error": error,
        },
    )

    if not pd_result.ok:
        logger.error(
            "PagerDuty escalation failed",
            extra={
                "incident_id": incident_id,
                "node_name": "escalation",
                "timestamp": "now",
                "pagerduty_error": pd_result.error,
            },
        )

    # ── Slack ──────────────────────────────────────────────────────────────────
    channel_id = os.environ.get("SLACK_ONCALL_CHANNEL_ID", "")
    thread_ts = state.get("approval_slack_thread_ts")

    slack_message = SlackMessage(
        channel_id=channel_id,
        text=summary,
        thread_ts=thread_ts,
    )
    await _deps.slack_client.post_message(slack_message)

    return {"escalated": True}
