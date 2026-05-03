"""Background task — monitors pending approvals for timeouts.

Runs every 60 seconds. For each incident in ``pending_approvals`` where
``timeout_at < NOW() AND status = 'pending'``, auto-rejects the incident
by resuming the LangGraph graph with ``Command(resume={"approved": False})``.

This is resilient to process restarts: on startup the task reads from Postgres
and catches up on any timeouts that fired while the process was down.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from langgraph.types import Command

if TYPE_CHECKING:
    from langgraph.graph.graph import CompiledGraph

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 60

_QUERY_TIMED_OUT = """
SELECT incident_id
FROM pending_approvals
WHERE status = 'pending' AND timeout_at < NOW()
FOR UPDATE SKIP LOCKED;
"""

_MARK_TIMED_OUT = """
UPDATE pending_approvals
SET status = 'timed_out'
WHERE incident_id = ANY(%s) AND status = 'pending';
"""


async def _auto_reject(graph: CompiledGraph, incident_id: str) -> None:
    """Resume the graph with a rejection decision for a timed-out approval."""
    config = {"configurable": {"thread_id": incident_id}}
    command: Command[Any] = Command(resume={"approved": False})
    try:
        await graph.ainvoke(command, config=config)
        logger.warning(
            "Approval timeout — auto-rejected",
            extra={"incident_id": incident_id},
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "Failed to auto-reject timed-out incident",
            extra={"incident_id": incident_id, "error": str(exc)},
            exc_info=True,
        )


async def monitor_approval_timeouts(graph: CompiledGraph) -> None:
    """Poll Postgres for timed-out approvals and auto-reject them.

    Intended to run as a long-lived asyncio background task started during
    application startup and cancelled during shutdown.

    Args:
        graph: The compiled IRAS LangGraph.
    """
    postgres_url = os.environ.get("POSTGRES_URL")

    if not postgres_url:
        logger.warning("POSTGRES_URL not set — approval timeout monitoring disabled")
        return

    import psycopg

    while True:
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)

        try:
            async with await psycopg.AsyncConnection.connect(postgres_url) as conn:
                async with conn.transaction():
                    async with conn.cursor() as cur:
                        await cur.execute(_QUERY_TIMED_OUT)
                        rows = await cur.fetchall()
                        timed_out_ids = [row[0] for row in rows]

                        if not timed_out_ids:
                            continue

                        await cur.execute(_MARK_TIMED_OUT, (timed_out_ids,))

            logger.info(
                "Found timed-out approvals",
                extra={"count": len(timed_out_ids), "incident_ids": timed_out_ids},
            )

            # Auto-reject each timed-out incident concurrently
            await asyncio.gather(
                *[_auto_reject(graph, incident_id) for incident_id in timed_out_ids],
                return_exceptions=True,
            )

        except asyncio.CancelledError:
            logger.info("Approval timeout monitor cancelled")
            return
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error in approval timeout monitor",
                extra={"error": str(exc)},
                exc_info=True,
            )
            # Continue on error — don't crash the background task
