"""Node 1 — Alert Ingestion.

Validates the raw webhook payload, generates a UUID incident ID, and initialises
the IncidentState. No LLM is called. Routes unconditionally to the triage node.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from iras.graph.state import IncidentState

logger = logging.getLogger(__name__)

# Minimum fields an alert payload must contain for the system to function.
_REQUIRED_ALERT_FIELDS: frozenset[str] = frozenset({"title", "timestamp"})


def ingestion_node(state: IncidentState) -> dict[str, Any]:
    """Validate the alert payload and initialise IncidentState.

    Raises:
        ValueError: If the alert payload is missing required fields.
    """
    payload = state.get("alert_payload", {})

    missing = _REQUIRED_ALERT_FIELDS - set(payload.keys())
    if missing:
        raise ValueError(f"Alert payload is missing required fields: {', '.join(sorted(missing))}")

    # B1 fix: honour a pre-set incident_id from state (set by the webhook route)
    # so that the state, the LangGraph thread_id, and the HTTP response all share
    # the same UUID. Fall back to generating a fresh one when run standalone.
    incident_id = state.get("incident_id") or str(uuid.uuid4())
    started_at = datetime.now(tz=UTC).isoformat()

    logger.info(
        "Incident ingested",
        extra={
            "incident_id": incident_id,
            "node_name": "ingestion",
            "timestamp": started_at,
            "alert_title": payload.get("title", ""),
        },
    )

    return {
        "incident_id": incident_id,
        "started_at": started_at,
        "rca_attempts": 0,
        "escalated": False,
        "human_approved": None,
        "triage": None,
        "context": None,
        "hypothesis": None,
        "remediation_plan": None,
        "post_mortem": None,
        "resolved_at": None,
        "approval_slack_thread_ts": None,
        "error": None,
    }
