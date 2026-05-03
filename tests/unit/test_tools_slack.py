"""Unit tests for iras.tools.slack — MockSlackClient and SlackClient helpers."""

# pylint: disable=missing-class-docstring,missing-function-docstring,import-outside-toplevel
from __future__ import annotations

from iras.tools.slack import (
    MockSlackClient,
    SlackClient,
    SlackMessage,
)


class TestMockSlackClient:
    async def test_post_message_returns_ok(self):
        client = MockSlackClient()
        msg = SlackMessage(channel_id="C123", text="Hello")
        result = await client.post_message(msg)
        assert result.ok is True
        assert result.channel == "C123"

    async def test_post_message_stores_message(self):
        client = MockSlackClient()
        msg = SlackMessage(channel_id="C123", text="Hello")
        await client.post_message(msg)
        assert client.posted_messages_count == 1
        assert client.messages[0].text == "Hello"

    async def test_multiple_messages_increment_counter(self):
        client = MockSlackClient()
        for i in range(3):
            await client.post_message(SlackMessage(channel_id="C", text=f"msg{i}"))
        assert client.posted_messages_count == 3

    async def test_ts_is_unique_per_message(self):
        client = MockSlackClient()
        r1 = await client.post_message(SlackMessage(channel_id="C", text="a"))
        r2 = await client.post_message(SlackMessage(channel_id="C", text="b"))
        assert r1.ts != r2.ts

    async def test_should_fail_returns_error(self):
        client = MockSlackClient(should_fail=True)
        msg = SlackMessage(channel_id="C", text="fail me")
        result = await client.post_message(msg)
        assert result.ok is False
        assert result.error != ""

    async def test_should_fail_does_not_store_message(self):
        client = MockSlackClient(should_fail=True)
        await client.post_message(SlackMessage(channel_id="C", text="x"))
        assert client.posted_messages_count == 0

    async def test_threaded_message(self):
        client = MockSlackClient()
        msg = SlackMessage(channel_id="C", text="threaded", thread_ts="1234.5678")
        result = await client.post_message(msg)
        assert result.ok is True
        assert client.messages[0].thread_ts == "1234.5678"

    async def test_message_with_blocks(self):
        client = MockSlackClient()
        blocks = [{"type": "section", "text": {"type": "plain_text", "text": "hi"}}]
        msg = SlackMessage(channel_id="C", text="with blocks", blocks=blocks)
        await client.post_message(msg)
        assert client.messages[0].blocks == blocks


class TestSlackClientRealClient:
    """Tests for the real SlackClient (lines 45-46, 50-71)."""

    async def test_post_message_success(self):
        """Covers SlackClient.__init__ and post_message success path."""
        import types
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_web_client = MagicMock()
        mock_response = {"ok": True, "ts": "1705312200.000001", "channel": "C123"}
        mock_web_client.chat_postMessage = AsyncMock(return_value=mock_response)

        fake_module = types.ModuleType("slack_sdk.web.async_client")
        setattr(fake_module, "AsyncWebClient", MagicMock(return_value=mock_web_client))

        with patch.dict("sys.modules", {"slack_sdk.web.async_client": fake_module}):
            client = SlackClient("xoxb-test-token")
            msg = SlackMessage(channel_id="C123", text="Hello")
            result = await client.post_message(msg)

        assert result.ok is True
        assert result.ts == "1705312200.000001"
        assert result.channel == "C123"

    async def test_post_message_with_blocks_and_thread(self):
        """Covers kwargs building with blocks and thread_ts."""
        import types
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_web_client = MagicMock()
        mock_response = {"ok": True, "ts": "1234.5678", "channel": "C456"}
        mock_web_client.chat_postMessage = AsyncMock(return_value=mock_response)

        fake_module = types.ModuleType("slack_sdk.web.async_client")
        setattr(fake_module, "AsyncWebClient", MagicMock(return_value=mock_web_client))

        with patch.dict("sys.modules", {"slack_sdk.web.async_client": fake_module}):
            client = SlackClient("xoxb-test-token")
            blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "Hi"}}]
            msg = SlackMessage(
                channel_id="C456",
                text="Threaded message",
                blocks=blocks,
                thread_ts="1234.0000",
            )
            result = await client.post_message(msg)

        assert result.ok is True
        call_kwargs = mock_web_client.chat_postMessage.call_args[1]
        assert "blocks" in call_kwargs
        assert "thread_ts" in call_kwargs

    async def test_post_message_api_exception_returns_error(self):
        """Covers the except branch in post_message."""
        import types
        from unittest.mock import AsyncMock, MagicMock, patch

        mock_web_client = MagicMock()
        mock_web_client.chat_postMessage = AsyncMock(side_effect=RuntimeError("Slack API error"))

        fake_module = types.ModuleType("slack_sdk.web.async_client")
        setattr(fake_module, "AsyncWebClient", MagicMock(return_value=mock_web_client))

        with patch.dict("sys.modules", {"slack_sdk.web.async_client": fake_module}):
            client = SlackClient("xoxb-test-token")
            msg = SlackMessage(channel_id="C789", text="Will fail")
            result = await client.post_message(msg)

        assert result.ok is False
        assert "Slack API error" in result.error


class TestSlackClientStaticMethods:
    def test_build_approval_blocks_contains_incident_id(self):
        blocks = SlackClient.build_approval_blocks(
            incident_id="INC-001",
            severity="P0",
            root_cause="DB exhaustion",
            plan_summary="Restart pods",
            approve_url="http://approve",
            reject_url="http://reject",
        )
        text = str(blocks)
        assert "INC-001" in text
        assert "P0" in text

    def test_build_approval_blocks_has_actions(self):
        blocks = SlackClient.build_approval_blocks(
            incident_id="INC-001",
            severity="P1",
            root_cause="OOM",
            plan_summary="Scale up",
            approve_url="http://a",
            reject_url="http://r",
        )
        block_types = [b["type"] for b in blocks]
        assert "actions" in block_types

    def test_build_postmortem_blocks_contains_duration(self):
        blocks = SlackClient.build_postmortem_blocks(
            incident_id="INC-042",
            severity="P0",
            duration_minutes=37,
            root_cause="Network partition",
            action_items=["Add circuit breaker", "Update runbook"],
        )
        text = str(blocks)
        assert "37" in text
        assert "INC-042" in text

    def test_build_postmortem_blocks_includes_action_items(self):
        blocks = SlackClient.build_postmortem_blocks(
            incident_id="INC-99",
            severity="P1",
            duration_minutes=15,
            root_cause="Disk full",
            action_items=["Clean disk", "Add monitoring"],
        )
        text = str(blocks)
        assert "Clean disk" in text
        assert "Add monitoring" in text

    def test_build_postmortem_blocks_empty_items(self):
        blocks = SlackClient.build_postmortem_blocks(
            incident_id="INC-0",
            severity="P2",
            duration_minutes=5,
            root_cause="Flaky test",
            action_items=[],
        )
        assert isinstance(blocks, list)
        assert len(blocks) > 0
