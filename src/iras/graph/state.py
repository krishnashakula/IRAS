"""LangGraph IncidentState — the shared mutable context for every graph run.

All Pydantic model fields are stored as plain dicts (via .model_dump()) so the
state is trivially JSON-serialisable by any LangGraph checkpointer backend.
Helper functions in each node convert back to typed models when needed.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import TypedDict


class IncidentState(TypedDict, total=False):
    """Shared mutable state passed between every node in the incident graph."""

    # ── Always set by the ingestion node ──────────────────────────────────────
    alert_payload: dict[str, Any]
    """Raw webhook payload that triggered this incident."""

    incident_id: str
    """UUID generated at ingestion time; used as LangGraph thread_id."""

    started_at: str
    """ISO-8601 datetime string when the incident was first received."""

    # ── Set by respective agent nodes (stored as dicts) ───────────────────────
    triage: dict[str, Any] | None
    """TriageResult.model_dump() — set by the triage node."""

    context: dict[str, Any] | None
    """ContextBundle.model_dump() — set by the context-gathering node."""

    hypothesis: dict[str, Any] | None
    """RootCauseHypothesis.model_dump() — set by the RCA node."""

    remediation_plan: dict[str, Any] | None
    """RemediationPlan.model_dump() — set by the generate-plan node."""

    post_mortem: dict[str, Any] | None
    """PostMortem.model_dump() — set by the post-mortem node."""

    # ── Control-flow fields ───────────────────────────────────────────────────
    rca_attempts: int
    """Number of RCA attempts so far; incremented by the RCA node."""

    human_approved: bool | None
    """None until the human responds; True/False after the approval interrupt."""

    escalated: bool
    """True if this incident was routed to manual escalation."""

    # ── Metadata ──────────────────────────────────────────────────────────────
    resolved_at: str | None
    """ISO-8601 datetime string when the post-mortem was finalised."""

    approval_slack_thread_ts: str | None
    """Slack message timestamp used to thread approval replies."""

    error: str | None
    """Last error message if a node caught a non-fatal exception."""
