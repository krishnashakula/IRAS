"""Tests for iras.graph.checkpointer module."""

# pylint: disable=missing-class-docstring,import-outside-toplevel,protected-access
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestGetCheckpointer:
    async def test_get_checkpointer_initialises(self):
        """get_checkpointer should create and return AsyncPostgresSaver."""
        mock_checkpointer = MagicMock()
        mock_checkpointer.setup = AsyncMock()

        # B4 fix: code now uses AsyncConnectionPool + AsyncPostgresSaver(conn=pool)
        mock_pool = MagicMock()
        mock_pool.open = AsyncMock()

        # get_checkpointer also connects directly for IRAS DDL tables
        mock_cursor = MagicMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=None)
        mock_cursor.execute = AsyncMock()
        mock_conn = MagicMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()

        with (
            patch(
                "iras.graph.checkpointer.AsyncConnectionPool",
                return_value=mock_pool,
            ) as mock_pool_cls,
            patch(
                "iras.graph.checkpointer.AsyncPostgresSaver",
                return_value=mock_checkpointer,
            ) as mock_saver_cls,
            patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=mock_conn)),
        ):
            import iras.graph.checkpointer as chk_module

            chk_module._checkpointer = None
            chk_module._init_lock = None
            result = await chk_module.get_checkpointer("postgresql://test")

        mock_pool_cls.assert_called_once()
        mock_pool.open.assert_called_once()
        mock_saver_cls.assert_called_once_with(conn=mock_pool)
        mock_checkpointer.setup.assert_called_once()
        assert result is mock_checkpointer

    async def test_get_checkpointer_returns_cached_on_second_call(self):
        """Calling get_checkpointer twice should return same instance."""
        mock_checkpointer = MagicMock()

        import iras.graph.checkpointer as chk_module

        original = chk_module._checkpointer
        try:
            chk_module._checkpointer = mock_checkpointer
            result = await chk_module.get_checkpointer("postgresql://test")
            assert result is mock_checkpointer
        finally:
            chk_module._checkpointer = original

    async def test_get_checkpointer_inner_lock_early_return(self):
        """Covers line 84: double-checked locking return inside async with _init_lock.

        Simulates a second concurrent caller that acquires the lock after the first
        caller has already set _checkpointer, so it returns the cached instance.
        """
        import asyncio

        import iras.graph.checkpointer as chk_module

        mock_checkpointer = MagicMock()
        lock = asyncio.Lock()

        original = chk_module._checkpointer
        original_lock = chk_module._init_lock
        try:
            # Use a real lock but hold it so the task will wait at "async with _init_lock"
            chk_module._init_lock = lock
            chk_module._checkpointer = None

            await lock.acquire()

            # Schedule get_checkpointer — it will pass the outer check (None) then block on lock
            task = asyncio.create_task(chk_module.get_checkpointer("postgresql://test"))

            # Yield so the task runs until it waits for the lock
            await asyncio.sleep(0)

            # While task is blocked, set _checkpointer to a value
            chk_module._checkpointer = mock_checkpointer

            # Release the lock: task acquires it, inner check finds _checkpointer != None → line 84
            lock.release()

            result = await task
            assert result is mock_checkpointer
        finally:
            chk_module._checkpointer = original
            chk_module._init_lock = original_lock


class TestCloseCheckpointer:
    async def test_close_when_none_is_noop(self):
        """close_checkpointer does nothing if _checkpointer is None."""
        import iras.graph.checkpointer as chk_module

        original = chk_module._checkpointer
        try:
            chk_module._checkpointer = None
            await chk_module.close_checkpointer()  # Should not raise
            assert chk_module._checkpointer is None
        finally:
            chk_module._checkpointer = original

    async def test_close_sets_to_none(self):
        """close_checkpointer resets _checkpointer to None."""
        mock_checkpointer = MagicMock()
        mock_checkpointer.conn = AsyncMock()
        mock_checkpointer.conn.close = AsyncMock()

        import iras.graph.checkpointer as chk_module

        original = chk_module._checkpointer
        try:
            chk_module._checkpointer = mock_checkpointer
            await chk_module.close_checkpointer()
            assert chk_module._checkpointer is None
        finally:
            chk_module._checkpointer = original

    async def test_close_handles_exception_gracefully(self):
        """If conn.close() raises, close_checkpointer swallows it."""
        mock_checkpointer = MagicMock()
        mock_checkpointer.conn = AsyncMock()
        mock_checkpointer.conn.close = AsyncMock(side_effect=Exception("conn error"))

        import iras.graph.checkpointer as chk_module

        original = chk_module._checkpointer
        try:
            chk_module._checkpointer = mock_checkpointer
            await chk_module.close_checkpointer()  # Should not raise
            assert chk_module._checkpointer is None
        finally:
            chk_module._checkpointer = original
