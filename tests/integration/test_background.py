"""Tests for background task and approval monitoring."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iras.api.background import _auto_reject, monitor_approval_timeouts


class TestAutoReject:
    async def test_auto_reject_calls_graph_ainvoke(self):
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={})
        await _auto_reject(mock_graph, "INC-001")
        mock_graph.ainvoke.assert_called_once()
        # Verify the call uses the right thread_id
        call_args = mock_graph.ainvoke.call_args
        # Either positional or keyword argument
        assert "INC-001" in str(call_args)

    async def test_auto_reject_handles_graph_exception(self):
        """Exception in graph.ainvoke should be caught and logged."""
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph error"))
        # Should not raise
        await _auto_reject(mock_graph, "INC-001")


class TestMonitorApprovalTimeouts:
    async def test_exits_immediately_without_postgres_url(self):
        """monitor_approval_timeouts should return early when POSTGRES_URL not set."""
        old = os.environ.pop("POSTGRES_URL", None)
        try:
            mock_graph = AsyncMock()
            # Should return quickly (not loop forever)
            await asyncio.wait_for(
                monitor_approval_timeouts(mock_graph),
                timeout=1.0,
            )
        except TimeoutError:
            pytest.fail("monitor_approval_timeouts did not exit without POSTGRES_URL")
        finally:
            if old:
                os.environ["POSTGRES_URL"] = old

    async def test_cancelled_error_exits_cleanly(self):
        """CancelledError should cause the monitor to exit gracefully."""
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=None)
        mock_cursor.execute = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.transaction = MagicMock()
        mock_conn.transaction.return_value.__aenter__ = AsyncMock(return_value=None)
        mock_conn.transaction.return_value.__aexit__ = AsyncMock(return_value=None)

        mock_graph = AsyncMock()

        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            task = asyncio.create_task(monitor_approval_timeouts(mock_graph))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            os.environ.pop("POSTGRES_URL", None)

    async def test_monitor_loop_body_empty_timed_out(self):
        """Covers lines 81-93: the while loop body with zero timed-out incidents."""

        mock_conn = MagicMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_transaction = MagicMock()
        mock_transaction.__aenter__ = AsyncMock(return_value=None)
        mock_transaction.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=mock_transaction)
        mock_cursor = MagicMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=None)
        mock_cursor.execute = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        mock_graph = AsyncMock()

        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=mock_conn)):
                with patch("iras.api.background._POLL_INTERVAL_SECONDS", 0):
                    task = asyncio.create_task(monitor_approval_timeouts(mock_graph))
                    # Yield to event loop multiple times so the loop body runs
                    for _ in range(20):
                        await asyncio.sleep(0)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        finally:
            os.environ.pop("POSTGRES_URL", None)

    async def test_monitor_loop_body_with_timed_out_incidents(self):
        """Covers lines 94-109: auto_reject called when timed-out incidents exist."""

        mock_conn = MagicMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_transaction = MagicMock()
        mock_transaction.__aenter__ = AsyncMock(return_value=None)
        mock_transaction.__aexit__ = AsyncMock(return_value=None)
        mock_conn.transaction = MagicMock(return_value=mock_transaction)
        mock_cursor = MagicMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=None)
        mock_cursor.execute = AsyncMock()
        # Return one timed-out incident on the first call, then empty
        call_count = [0]

        async def fetchall_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return [("INC-001",)]
            return []

        mock_cursor.fetchall = fetchall_side_effect
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={})

        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=mock_conn)):
                with patch("iras.api.background._POLL_INTERVAL_SECONDS", 0):
                    task = asyncio.create_task(monitor_approval_timeouts(mock_graph))
                    for _ in range(30):
                        await asyncio.sleep(0)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            # auto_reject should have been called for INC-001
            assert mock_graph.ainvoke.call_count >= 1
        finally:
            os.environ.pop("POSTGRES_URL", None)

    async def test_monitor_loop_handles_db_exception(self):
        """Covers the except Exception branch in the monitor loop."""
        mock_graph = AsyncMock()

        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch(
                "psycopg.AsyncConnection.connect",
                AsyncMock(side_effect=RuntimeError("DB unreachable")),
            ):
                with patch("iras.api.background._POLL_INTERVAL_SECONDS", 0):
                    task = asyncio.create_task(monitor_approval_timeouts(mock_graph))
                    for _ in range(10):
                        await asyncio.sleep(0)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
        finally:
            os.environ.pop("POSTGRES_URL", None)
