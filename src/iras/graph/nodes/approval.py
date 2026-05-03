"""Node 5b — Human Approval Checkpoint.

Uses LangGraph's interrupt() mechanism to pause the graph and wait for a human
decision. The graph resumes when the FastAPI /approve or /reject endpoint
injects a Command(resume={...}) via the graph runner.

Execution flow:
  First run:  interrupt({...}) raises NodeInterrupt → graph pauses
  On resume:  interrupt({...}) returns the resume value → node continues

The remediation_plan is already in state (set by generate_plan_node) when this
node runs, so no plan regeneration occurs on the second execution.

If generate_plan_node wrote an error, this node routes directly to escalation
by setting human_approved=False without calling interrupt().
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from iras.graph.state import IncidentState

logger = logging.getLogger(__name__)


def approval_node(state: IncidentState) -> dict[str, Any]:
    """Wait for human approval of the remediation plan.

    On first execution, raises NodeInterrupt to pause the graph.
    On resume, returns the decision provided via Command(resume={...}).

    The resume value must be a dict with an ``approved`` boolean key:
        Command(resume={"approved": True})   # approve
        Command(resume={"approved": False})  # reject / timeout

    Returns:
        State update dict with ``human_approved`` populated.
    """
    incident_id = state.get("incident_id", "unknown")

    # If plan generation failed, skip the interrupt and route to escalation
    if state.get("error") and state.get("remediation_plan") is None:
        logger.warning(
            "Skipping approval interrupt — plan generation failed",
            extra={"incident_id": incident_id, "node_name": "approval", "timestamp": "now"},
        )
        return {"human_approved": False}

    plan = state.get("remediation_plan") or {}
    triage = state.get("triage") or {}

    logger.info(
        "Waiting for human approval",
        extra={"incident_id": incident_id, "node_name": "approval", "timestamp": "now"},
    )

    # This either raises NodeInterrupt (first run) or returns the resume value
    decision: dict[str, Any] = interrupt(
        {
            "incident_id": incident_id,
            "type": "approval_required",
            "severity": triage.get("severity", "unknown"),
            "plan_summary": plan.get("summary", ""),
            "requires_human_approval": plan.get("requires_human_approval", True),
        }
    )

    approved = bool(decision.get("approved", False))

    logger.info(
        "Human approval received",
        extra={
            "incident_id": incident_id,
            "node_name": "approval",
            "timestamp": "now",
            "approved": approved,
        },
    )

    return {"human_approved": approved}


def route_after_approval(state: IncidentState) -> str:
    """LangGraph conditional edge — determines next node after approval.

    Returns:
        ``"apply_remediation"`` if approved, ``"escalation"`` otherwise.
    """
    if state.get("human_approved") is True:
        return "apply_remediation"
    return "escalation"
