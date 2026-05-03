"""Node 2 — Triage Agent Node.

Delegates to the Pydantic AI triage agent (Claude Haiku) and writes the
validated TriageResult to the IncidentState. Routes unconditionally to the
context-gathering node.

If the agent raises after exhausting retries, the exception propagates to
LangGraph which surfaces it as a graph error.
"""

from __future__ import annotations

import logging
from typing import Any

from iras.agents.deps import TriageDeps
from iras.agents.triage import run_triage
from iras.graph.state import IncidentState

logger = logging.getLogger(__name__)


async def triage_node(
    state: IncidentState,
    *,
    deps: TriageDeps | None = None,
) -> dict[str, Any]:
    """Run the triage agent and persist the TriageResult to state.

    Args:
        state: Current IncidentState.
        deps: Optional TriageDeps injection (useful for testing).

    Returns:
        State update dict with the ``triage`` field populated.
    """
    incident_id = state.get("incident_id", "unknown")
    payload = state.get("alert_payload", {})

    logger.info(
        "Starting triage",
        extra={"incident_id": incident_id, "node_name": "triage", "timestamp": "now"},
    )

    triage_result = await run_triage(payload, deps=deps or TriageDeps())

    logger.info(
        "Triage complete",
        extra={
            "incident_id": incident_id,
            "node_name": "triage",
            "timestamp": "now",
            "severity": triage_result.severity,
            "affected_services": triage_result.affected_services,
            "confidence": triage_result.confidence,
        },
    )

    return {"triage": triage_result.model_dump()}
