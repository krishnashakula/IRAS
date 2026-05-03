"""Postgres checkpointer singleton for LangGraph.

Manages the lifecycle of an AsyncPostgresSaver (from
langgraph-checkpoint-postgres) — initialised once per process and reused
across all graph runs.

Side-effects on first init:
  1. Creates the LangGraph checkpoint tables (via checkpointer.setup())
  2. Creates the IRAS-specific tables (postmortems, pending_approvals)
"""

from __future__ import annotations

import asyncio
import logging

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import AsyncConnectionPool

logger = logging.getLogger(__name__)

_checkpointer: AsyncPostgresSaver | None = None  # pylint: disable=invalid-name
_pool: AsyncConnectionPool | None = None  # pylint: disable=invalid-name
_init_lock: asyncio.Lock | None = None  # pylint: disable=invalid-name

# ── IRAS-specific DDL ─────────────────────────────────────────────────────────

_CREATE_POSTMORTEMS_SQL = """
CREATE TABLE IF NOT EXISTS postmortems (
    incident_id          TEXT PRIMARY KEY,
    severity             TEXT NOT NULL,
    started_at           TIMESTAMPTZ,
    resolved_at          TIMESTAMPTZ,
    root_cause_summary   TEXT,
    resolution_summary   TEXT,
    timeline             JSONB,
    action_items         JSONB,
    total_duration_minutes INTEGER,
    raw_state            JSONB,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_PENDING_APPROVALS_SQL = """
CREATE TABLE IF NOT EXISTS pending_approvals (
    incident_id  TEXT PRIMARY KEY,
    thread_id    TEXT NOT NULL,
    severity     TEXT NOT NULL,
    timeout_at   TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_status_timeout
    ON pending_approvals (status, timeout_at);
"""


async def get_checkpointer(conn_string: str) -> AsyncPostgresSaver:
    """Return the singleton AsyncPostgresSaver, initialising it if needed.

    Creates all required tables on first call.

    Args:
        conn_string: psycopg-compatible Postgres connection string.

    Returns:
        Configured AsyncPostgresSaver ready for use.
    """
    global _checkpointer, _init_lock  # pylint: disable=global-statement

    if _checkpointer is not None:
        return _checkpointer

    # B4 fix: use a lock so concurrent callers only initialise one connection pool
    if _init_lock is None:
        _init_lock = asyncio.Lock()

    async with _init_lock:
        # Re-check inside lock — another coroutine may have initialised while we waited
        if _checkpointer is not None:
            return _checkpointer

        logger.info("Initialising Postgres checkpointer")

        pool = AsyncConnectionPool(
            conn_string,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        await pool.open()

        checkpointer = AsyncPostgresSaver(conn=pool)  # type: ignore[arg-type]
        # Creates LangGraph checkpoint tables (checkpoints, checkpoint_writes, etc.)
        await checkpointer.setup()

        # Create IRAS-specific tables
        import psycopg

        async with await psycopg.AsyncConnection.connect(conn_string) as conn:
            async with conn.cursor() as cur:
                await cur.execute(_CREATE_POSTMORTEMS_SQL)
                await cur.execute(_CREATE_PENDING_APPROVALS_SQL)
            await conn.commit()

        global _pool  # pylint: disable=global-statement
        _pool = pool
        _checkpointer = checkpointer  # type: ignore[assignment]
        logger.info("Postgres checkpointer ready")
        return _checkpointer  # type: ignore[return-value]


async def close_checkpointer() -> None:
    """Close the singleton checkpointer connection pool.

    Should be called on application shutdown.
    """
    global _checkpointer, _pool  # pylint: disable=global-statement
    if _pool is not None:
        try:
            await _pool.close()
        except Exception:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            pass
        _pool = None
    _checkpointer = None
    logger.info("Postgres checkpointer closed")
