"""Slack tool — sends structured messages and supports message threading.

Used by:
- The generate-plan node to notify on-call of a pending remediation approval
- The post-mortem node to post the incident summary to the incident channel

All approval follow-up messages are threaded to the original notification.
"""

# pylint: disable=missing-class-docstring,missing-function-docstring,import-outside-toplevel,broad-exception-caught,too-many-arguments,too-many-positional-arguments
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Domain types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlackMessage:
    channel_id: str
    text: str
    blocks: list[dict[str, Any]] = field(default_factory=list)
    thread_ts: str | None = None
    """When set, the message is posted as a reply in the given thread."""


@dataclass(frozen=True)
class SlackPostResult:
    ok: bool
    ts: str
    """Message timestamp — use as thread_ts for replies."""
    channel: str
    error: str = ""


# ── Real Slack client ─────────────────────────────────────────────────────────


class SlackClient:
    """Thin wrapper around the Slack Web API for incident notifications."""

    def __init__(self, bot_token: str) -> None:
        from slack_sdk.web.async_client import AsyncWebClient

        self._client = AsyncWebClient(token=bot_token)

    async def post_message(self, message: SlackMessage) -> SlackPostResult:
        """Post *message* to Slack and return the post result with thread ts."""
        try:
            kwargs: dict[str, Any] = {
                "channel": message.channel_id,
                "text": message.text,
            }
            if message.blocks:
                kwargs["blocks"] = message.blocks
            if message.thread_ts:
                kwargs["thread_ts"] = message.thread_ts

            resp = await self._client.chat_postMessage(**kwargs)
            return SlackPostResult(
                ok=bool(resp.get("ok", False)),
                ts=str(resp.get("ts", "")),
                channel=str(resp.get("channel", "")),
            )
        except Exception as exc:
            logger.error(
                "Slack post_message failed",
                extra={"channel": message.channel_id, "error": str(exc)},
            )
            return SlackPostResult(ok=False, ts="", channel=message.channel_id, error=str(exc))

    @staticmethod
    def build_approval_blocks(
        incident_id: str,
        severity: str,
        root_cause: str,
        plan_summary: str,
        approve_url: str,
        reject_url: str,
    ) -> list[dict[str, Any]]:
        """Build Slack Block Kit layout for a remediation approval request."""
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"🚨 [{severity}] Incident {incident_id} — Approval Required",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Root Cause:*\n{root_cause}",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Remediation Plan:*\n{plan_summary}",
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "✅ Approve"},
                        "style": "primary",
                        "url": approve_url,
                        "action_id": "approve_remediation",
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "❌ Reject"},
                        "style": "danger",
                        "url": reject_url,
                        "action_id": "reject_remediation",
                    },
                ],
            },
        ]

    @staticmethod
    def build_postmortem_blocks(
        incident_id: str,
        severity: str,
        duration_minutes: int,
        root_cause: str,
        action_items: list[str],
    ) -> list[dict[str, Any]]:
        """Build Slack Block Kit layout for a post-mortem summary."""
        items_text = "\n".join(f"• {item}" for item in action_items)
        return [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"📋 Post-Mortem: {incident_id} [{severity}]",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Duration:*\n{duration_minutes} min"},
                    {"type": "mrkdwn", "text": f"*Severity:*\n{severity}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Root Cause:*\n{root_cause}"},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Action Items:*\n{items_text}"},
            },
        ]


# ── Mock Slack client ─────────────────────────────────────────────────────────


class MockSlackClient:
    """In-memory mock for unit and integration tests."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.messages: list[SlackMessage] = []
        self._ts_counter: int = 0

    @property
    def posted_messages_count(self) -> int:
        return len(self.messages)

    async def post_message(self, message: SlackMessage) -> SlackPostResult:
        if self.should_fail:
            return SlackPostResult(
                ok=False, ts="", channel=message.channel_id, error="Mock failure"
            )
        self.messages.append(message)
        self._ts_counter += 1
        ts = f"1700000000.{self._ts_counter:06d}"
        return SlackPostResult(ok=True, ts=ts, channel=message.channel_id)
