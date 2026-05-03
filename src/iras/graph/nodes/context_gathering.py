"""Node 3 — Context Gathering Agent Node.

Delegates to the Pydantic AI context agent (Claude Haiku with tools) and writes
the validated ContextBundle to the IncidentState. If any tool fails, the agent
includes the failure in the ContextBundle summary and continues.

Routes unconditionally to the RCA node.
"""

from __future__ import annotations

import logging
from typing import Any

from iras.agents.context_gathering import ContextDeps, run_context_gathering
from iras.graph.state import IncidentState

logger = logging.getLogger(__name__)


async def context_gathering_node(
    state: IncidentState,
    *,
    deps: ContextDeps | None = None,
) -> dict[str, Any]:
    """Run the context-gathering agent and persist the ContextBundle to state.

    Args:
        state: Current IncidentState.
        deps: Optional ContextDeps injection (useful for testing).

    Returns:
        State update dict with the ``context`` field populated.
    """
    incident_id = state.get("incident_id", "unknown")
    rca_attempts = state.get("rca_attempts", 0)

    logger.info(
        "Starting context gathering",
        extra={
            "incident_id": incident_id,
            "node_name": "context_gathering",
            "timestamp": "now",
            "rca_attempt": rca_attempts,
        },
    )

    context_bundle = await run_context_gathering(
        alert_payload=state.get("alert_payload", {}),
        triage=state.get("triage") or {},
        deps=deps,
    )

    failed = context_bundle.failed_tools
    if failed:
        logger.warning(
            "Context gathering had tool failures",
            extra={
                "incident_id": incident_id,
                "node_name": "context_gathering",
                "timestamp": "now",
                "failed_tools": failed,
            },
        )

    logger.info(
        "Context gathering complete",
        extra={
            "incident_id": incident_id,
            "node_name": "context_gathering",
            "timestamp": "now",
            "log_evidence_count": len(context_bundle.log_evidence),
            "metric_anomaly_count": len(context_bundle.metric_anomalies),
            "deployment_count": len(context_bundle.recent_deployments),
        },
    )

    return {"context": context_bundle.model_dump()}
