"""Node 4 — Root Cause Analysis Agent Node.

Delegates to the Pydantic AI RCA agent (Claude Sonnet). Increments the
rca_attempts counter. The confidence score and attempt count drive LangGraph
conditional routing:

  confidence >= threshold AND attempts < max  →  generate_plan
  confidence < threshold AND attempts < max   →  context_gathering (retry)
  confidence < threshold AND attempts >= max  →  escalation
  agent raises after all retries              →  escalation

If the Pydantic AI agent exhausts its validation retries (3 attempts), the
exception is caught here and the node routes to manual escalation by setting
``error`` in the state.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic_ai.exceptions import UnexpectedModelBehavior

from iras.agents.deps import RCADeps
from iras.agents.rca import run_rca
from iras.config.settings import get_settings
from iras.graph.state import IncidentState

logger = logging.getLogger(__name__)


async def rca_node(
    state: IncidentState,
    *,
    deps: RCADeps | None = None,
) -> dict[str, Any]:
    """Run the RCA agent and persist the RootCauseHypothesis to state.

    Increments rca_attempts regardless of outcome so the retry cap is enforced
    even when the agent raises.

    Args:
        state: Current IncidentState.
        deps: Optional RCADeps injection (useful for testing).

    Returns:
        State update dict with ``hypothesis``, ``rca_attempts``, and optionally
        ``error`` fields populated.
    """
    incident_id = state.get("incident_id", "unknown")
    new_attempts = (state.get("rca_attempts") or 0) + 1

    logger.info(
        "Starting RCA",
        extra={
            "incident_id": incident_id,
            "node_name": "rca",
            "timestamp": "now",
            "rca_attempt": new_attempts,
        },
    )

    try:
        hypothesis = await run_rca(
            alert_payload=state.get("alert_payload", {}),
            triage=state.get("triage") or {},
            context=state.get("context") or {},
            deps=deps,
        )

        logger.info(
            "RCA complete",
            extra={
                "incident_id": incident_id,
                "node_name": "rca",
                "timestamp": "now",
                "confidence": hypothesis.confidence,
                "rca_attempts": new_attempts,
                "primary_cause": hypothesis.primary_cause[:120],
            },
        )

        return {
            "hypothesis": hypothesis.model_dump(),
            "rca_attempts": new_attempts,
            "error": None,
        }

    except UnexpectedModelBehavior as exc:
        settings = get_settings()
        logger.error(
            "RCA agent exhausted validation retries — routing to escalation",
            extra={
                "incident_id": incident_id,
                "node_name": "rca",
                "timestamp": "now",
                "error": str(exc),
                "rca_attempts": new_attempts,
            },
        )
        # Force the retry cap so the graph routes to escalation immediately
        return {
            "hypothesis": None,
            "rca_attempts": settings.rca_max_attempts,  # exhausted
            "error": f"RCA agent validation failed: {exc}",
        }


def route_after_rca(state: IncidentState) -> str:
    """LangGraph conditional edge — determines next node after RCA.

    Returns:
        Node name: ``"generate_plan"``, ``"context_gathering"``, or ``"escalation"``.
    """
    settings = get_settings()
    threshold = settings.rca_confidence_threshold
    max_attempts = settings.rca_max_attempts
    attempts = state.get("rca_attempts") or 0
    error = state.get("error")

    # Agent failed — force escalation
    if error and state.get("hypothesis") is None:
        return "escalation"

    hypothesis = state.get("hypothesis") or {}
    confidence = float(hypothesis.get("confidence", 0.0))

    if confidence >= threshold:
        return "generate_plan"

    if attempts < max_attempts:
        return "context_gathering"

    return "escalation"
