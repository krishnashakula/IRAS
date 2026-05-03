"""Node 8 — Post-Mortem Generator (terminal node).

Delegates to the Pydantic AI post-mortem agent (Claude Sonnet) to produce a
structured PostMortem from the complete IncidentState.

After generation:
  1. Writes the PostMortem to Postgres (dedicated incidents table)
  2. Sends a summary to the incident Slack channel
  3. Sets resolved_at in the state

This is the terminal node — no outgoing edges.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from iras.agents.postmortem import PostMortemDeps, run_postmortem
from iras.graph.state import IncidentState
from iras.tools.slack import MockSlackClient, SlackClient, SlackMessage

logger = logging.getLogger(__name__)

# SQL to persist a post-mortem — executed against the shared Postgres connection
_INSERT_POSTMORTEM_SQL = """
INSERT INTO postmortems (
    incident_id, severity, started_at, resolved_at,
    root_cause_summary, resolution_summary, timeline,
    action_items, total_duration_minutes, raw_state
)
VALUES (
    %(incident_id)s, %(severity)s, %(started_at)s, %(resolved_at)s,
    %(root_cause_summary)s, %(resolution_summary)s, %(timeline)s,
    %(action_items)s, %(total_duration_minutes)s, %(raw_state)s
)
ON CONFLICT (incident_id) DO UPDATE
SET
    severity = EXCLUDED.severity,
    resolved_at = EXCLUDED.resolved_at,
    root_cause_summary = EXCLUDED.root_cause_summary,
    resolution_summary = EXCLUDED.resolution_summary,
    timeline = EXCLUDED.timeline,
    action_items = EXCLUDED.action_items,
    total_duration_minutes = EXCLUDED.total_duration_minutes,
    raw_state = EXCLUDED.raw_state;
"""


async def _persist_postmortem(postmortem: dict[str, Any], raw_state: dict[str, Any]) -> None:
    """Write the post-mortem to Postgres.

    Skips gracefully if POSTGRES_URL is not set (e.g. in unit tests).
    """
    postgres_url = os.environ.get("POSTGRES_URL")
    if not postgres_url:
        logger.debug("POSTGRES_URL not set — skipping post-mortem persistence")
        return

    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(postgres_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    _INSERT_POSTMORTEM_SQL,
                    {
                        "incident_id": postmortem["incident_id"],
                        "severity": postmortem["severity"],
                        "started_at": raw_state.get("started_at"),
                        "resolved_at": raw_state.get("resolved_at"),
                        "root_cause_summary": postmortem["root_cause_summary"],
                        "resolution_summary": postmortem["resolution_summary"],
                        "timeline": json.dumps(postmortem["timeline"]),
                        "action_items": json.dumps(postmortem["action_items"]),
                        "total_duration_minutes": postmortem["total_duration_minutes"],
                        "raw_state": json.dumps(raw_state, default=str),
                    },
                )
            await conn.commit()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "Failed to persist post-mortem to Postgres",
            extra={"incident_id": postmortem["incident_id"], "error": str(exc)},
        )


async def postmortem_node(
    state: IncidentState,
    *,
    deps: PostMortemDeps | None = None,
    slack_client: SlackClient | MockSlackClient | None = None,
) -> dict[str, Any]:
    """Generate, persist, and announce the post-mortem.

    Args:
        state: Complete IncidentState at end of graph.
        deps: Optional PostMortemDeps injection (useful for testing).
        slack_client: Optional Slack client override (useful for testing).

    Returns:
        State update dict with ``post_mortem`` and ``resolved_at`` populated.
    """
    incident_id = state.get("incident_id", "unknown")

    logger.info(
        "Generating post-mortem",
        extra={"incident_id": incident_id, "node_name": "postmortem", "timestamp": "now"},
    )

    postmortem = await run_postmortem(
        incident_state=dict(state),
        deps=deps,
    )

    resolved_at = datetime.now(tz=UTC).isoformat()

    logger.info(
        "Post-mortem generated",
        extra={
            "incident_id": incident_id,
            "node_name": "postmortem",
            "timestamp": "now",
            "duration_minutes": postmortem.total_duration_minutes,
            "action_items": len(postmortem.action_items),
        },
    )

    # ── Persist to Postgres ───────────────────────────────────────────────────
    pm_dict = postmortem.model_dump()
    # B2 fix: always use the incident_id from state (model may return a placeholder)
    pm_dict["incident_id"] = incident_id
    await _persist_postmortem(pm_dict, dict(state))

    # ── Send Slack summary ────────────────────────────────────────────────────
    channel_id = os.environ.get("SLACK_ONCALL_CHANNEL_ID", "")
    thread_ts = state.get("approval_slack_thread_ts")

    _slack = slack_client or _build_slack_client()

    blocks = SlackClient.build_postmortem_blocks(
        incident_id=incident_id,
        severity=postmortem.severity,
        duration_minutes=postmortem.total_duration_minutes,
        root_cause=postmortem.root_cause_summary,
        action_items=postmortem.action_items,
    )

    await _slack.post_message(
        SlackMessage(
            channel_id=channel_id,
            text=f"Post-mortem ready: {incident_id} [{postmortem.severity}]",
            blocks=blocks,
            thread_ts=thread_ts,
        )
    )

    return {
        "post_mortem": pm_dict,
        "resolved_at": resolved_at,
    }


def _build_slack_client() -> SlackClient | MockSlackClient:
    """Return a real or mock Slack client based on environment."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return SlackClient(token)
    return MockSlackClient()
