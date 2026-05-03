"""Node 6 — Apply Remediation.

Executes each RemediationStep in sequence. After each step:
- Verifies success (currently a hook for real implementation)
- On failure: rolls back the current step and all completed steps in reverse order

Only executes when human_approved is True (enforced by the conditional edge
from the approval_node).

Routes unconditionally to the post-mortem node.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from iras.graph.state import IncidentState
from iras.models.incident import RemediationPlan, RemediationStep

logger = logging.getLogger(__name__)


async def _execute_step(step: RemediationStep, incident_id: str) -> bool:
    """Execute a single remediation step.

    In production, this would run the step against the real infrastructure
    (kubectl, helm, terraform, etc.). Here we simulate execution with a short
    delay and assume success.

    Returns:
        True if the step succeeded, False otherwise.
    """
    logger.info(
        "Executing remediation step",
        extra={
            "incident_id": incident_id,
            "node_name": "apply_remediation",
            "timestamp": "now",
            "action": step.action[:120],
            "risk_level": step.risk_level,
            "estimated_duration_seconds": step.estimated_duration_seconds,
        },
    )
    # Simulate step execution time (capped at 2s for tests/demos)
    await asyncio.sleep(min(step.estimated_duration_seconds * 0.01, 2.0))
    return True  # Override in subclass or integration tests


async def _rollback_step(step: RemediationStep, incident_id: str) -> None:
    """Execute the rollback command for a step."""
    logger.warning(
        "Rolling back remediation step",
        extra={
            "incident_id": incident_id,
            "node_name": "apply_remediation",
            "timestamp": "now",
            "rollback_command": step.rollback_command[:120],
        },
    )
    await asyncio.sleep(0.01)  # Simulate rollback


async def apply_remediation_node(state: IncidentState) -> dict[str, Any]:
    """Execute all remediation steps with rollback on failure.

    Args:
        state: Current IncidentState. human_approved must be True.

    Returns:
        State update dict. Sets ``error`` if remediation failed.
    """
    incident_id = state.get("incident_id", "unknown")

    plan_dict = state.get("remediation_plan")
    if not plan_dict:
        logger.error(
            "apply_remediation called with no plan — routing to error",
            extra={
                "incident_id": incident_id,
                "node_name": "apply_remediation",
                "timestamp": "now",
            },
        )
        return {"error": "No remediation plan available", "escalated": True}

    plan = RemediationPlan.model_validate(plan_dict)
    completed: list[RemediationStep] = []

    logger.info(
        "Starting remediation",
        extra={
            "incident_id": incident_id,
            "node_name": "apply_remediation",
            "timestamp": "now",
            "total_steps": len(plan.steps),
        },
    )

    for i, step in enumerate(plan.steps, start=1):
        success = await _execute_step(step, incident_id)

        if not success:
            logger.error(
                "Remediation step failed — initiating rollback",
                extra={
                    "incident_id": incident_id,
                    "node_name": "apply_remediation",
                    "timestamp": "now",
                    "failed_step": i,
                    "action": step.action[:120],
                },
            )
            # Rollback failed step and all previously completed steps in reverse order
            for rollback_step in reversed([step, *completed]):
                await _rollback_step(rollback_step, incident_id)

            error_msg = f"Remediation failed at step {i}: {step.action}"
            return {"error": error_msg, "escalated": True}

        completed.append(step)
        logger.info(
            "Remediation step succeeded",
            extra={
                "incident_id": incident_id,
                "node_name": "apply_remediation",
                "timestamp": "now",
                "step": i,
                "total": len(plan.steps),
            },
        )

    logger.info(
        "Remediation completed successfully",
        extra={
            "incident_id": incident_id,
            "node_name": "apply_remediation",
            "timestamp": "now",
            "steps_completed": len(completed),
        },
    )
    return {"error": None}
