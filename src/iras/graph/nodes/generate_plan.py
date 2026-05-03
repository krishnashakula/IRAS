"""Node 5a — Generate Remediation Plan and Notify Slack.

Calls the Pydantic AI remediation agent (Claude Sonnet) to produce a typed
RemediationPlan, then sends a structured Slack notification to the on-call
channel with approve/reject action buttons.

This node runs to completion and commits its state update (remediation_plan,
approval_slack_thread_ts) BEFORE the approval_node calls interrupt(). This
ensures the plan is available in state when the graph resumes after the
human responds.

Routes unconditionally to the approval_node.
"""

# pylint: disable=too-many-locals,import-outside-toplevel
from __future__ import annotations

import logging
import os
from typing import Any

from pydantic_ai.exceptions import UnexpectedModelBehavior

from iras.agents.deps import ApprovalDeps, RemediationDeps
from iras.agents.remediation import run_remediation_planning
from iras.graph.state import IncidentState
from iras.tools.slack import SlackMessage

logger = logging.getLogger(__name__)


async def generate_plan_node(
    state: IncidentState,
    *,
    approval_deps: ApprovalDeps | None = None,
    remediation_deps: RemediationDeps | None = None,
) -> dict[str, Any]:
    """Generate the remediation plan and send a Slack approval notification.

    Args:
        state: Current IncidentState.
        approval_deps: Slack client injection (useful for testing).
        remediation_deps: Remediation agent deps injection (useful for testing).

    Returns:
        State update dict with ``remediation_plan`` and
        ``approval_slack_thread_ts`` populated.
    """
    incident_id = state.get("incident_id", "unknown")
    _approval_deps = approval_deps or ApprovalDeps()
    _remediation_deps = remediation_deps or RemediationDeps()

    logger.info(
        "Generating remediation plan",
        extra={"incident_id": incident_id, "node_name": "generate_plan", "timestamp": "now"},
    )

    try:
        plan = await run_remediation_planning(
            alert_payload=state.get("alert_payload", {}),
            triage=state.get("triage") or {},
            context=state.get("context") or {},
            hypothesis=state.get("hypothesis") or {},
            deps=_remediation_deps,
        )
    except UnexpectedModelBehavior as exc:
        logger.error(
            "Remediation agent failed — routing to escalation",
            extra={
                "incident_id": incident_id,
                "node_name": "generate_plan",
                "timestamp": "now",
                "error": str(exc),
            },
        )
        # Write a sentinel so approval_node skips the interrupt and routes to escalation
        return {"error": f"Remediation planning failed: {exc}", "remediation_plan": None}

    logger.info(
        "Remediation plan generated",
        extra={
            "incident_id": incident_id,
            "node_name": "generate_plan",
            "timestamp": "now",
            "step_count": len(plan.steps),
            "requires_human_approval": plan.requires_human_approval,
        },
    )

    # ── Build Slack approval notification ────────────────────────────────────
    triage = state.get("triage") or {}
    severity = triage.get("severity", "unknown")
    hypothesis = state.get("hypothesis") or {}
    root_cause = hypothesis.get("primary_cause", "Unknown root cause")

    base_url = os.environ.get("IRAS_BASE_URL", "http://localhost:8000")
    approve_url = f"{base_url}/incidents/{incident_id}/approve"
    reject_url = f"{base_url}/incidents/{incident_id}/reject"

    from iras.tools.slack import SlackClient

    channel_id = os.environ.get("SLACK_ONCALL_CHANNEL_ID", "")

    blocks = SlackClient.build_approval_blocks(
        incident_id=incident_id,
        severity=severity,
        root_cause=root_cause,
        plan_summary=plan.summary,
        approve_url=approve_url,
        reject_url=reject_url,
    )

    message = SlackMessage(
        channel_id=channel_id,
        text=f"[{severity}] Incident {incident_id} — Remediation Approval Required",
        blocks=blocks,
    )

    slack_result = await _approval_deps.slack_client.post_message(message)

    if not slack_result.ok:
        logger.warning(
            "Slack notification failed — continuing without thread ts",
            extra={
                "incident_id": incident_id,
                "node_name": "generate_plan",
                "timestamp": "now",
                "slack_error": slack_result.error,
            },
        )

    return {
        "remediation_plan": plan.model_dump(),
        "approval_slack_thread_ts": slack_result.ts if slack_result.ok else None,
        "error": None,
    }
