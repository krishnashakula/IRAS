"""Integration tests for the postmortem node."""
# pylint: disable=redefined-outer-name
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.deps import PostMortemDeps
from iras.agents.postmortem import postmortem_agent
from iras.graph.nodes.postmortem import postmortem_node
from iras.graph.state import IncidentState
from iras.models.incident import PostMortem
from iras.tools.slack import MockSlackClient


def make_postmortem_fn(incident_id: str = "test-001"):
    """Return a FunctionModel callback that produces a canned PostMortem response."""
    def fn(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        payload = {
            "incident_id": incident_id,
            "timeline": [
                "10:30 UTC — Alert fired",
                "10:31 UTC — Triage complete",
                "10:40 UTC — Remediation applied",
                "10:46 UTC — Resolved",
            ],
            "root_cause_summary": "DB connection pool exhaustion.",
            "resolution_summary": "Increased pool size and restarted service.",
            "action_items": [
                "Add automated pool scaling",
                "Add pool exhaustion alert",
            ],
            "severity": "P1",
            "total_duration_minutes": 16,
        }
        return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

    return fn


@pytest.fixture
def postmortem_state(base_incident_state) -> IncidentState:
    """Return the base_incident_state as-is for postmortem node tests."""
    return base_incident_state


class TestPostMortemNode:
    """Integration tests for the postmortem_node graph node."""
    async def test_writes_post_mortem_to_state(self, postmortem_state: IncidentState):
        """Node must populate the post_mortem key in its output dict."""
        mock_slack = MockSlackClient()
        with postmortem_agent.override(model=FunctionModel(make_postmortem_fn())):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                updates = await postmortem_node(
                    postmortem_state,
                    deps=PostMortemDeps(),
                    slack_client=mock_slack,
                )

        assert "post_mortem" in updates
        assert updates["post_mortem"] is not None

    async def test_sets_resolved_at(self, postmortem_state: IncidentState):
        """Node must set resolved_at to a valid ISO-8601 datetime string."""
        import re

        mock_slack = MockSlackClient()
        with postmortem_agent.override(model=FunctionModel(make_postmortem_fn())):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                updates = await postmortem_node(
                    postmortem_state,
                    deps=PostMortemDeps(),
                    slack_client=mock_slack,
                )

        assert updates.get("resolved_at") is not None
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", updates["resolved_at"])

    async def test_slack_summary_sent(self, postmortem_state: IncidentState):
        """Node must post at least one Slack message with the post-mortem summary."""
        mock_slack = MockSlackClient()
        with postmortem_agent.override(model=FunctionModel(make_postmortem_fn())):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                await postmortem_node(
                    postmortem_state,
                    deps=PostMortemDeps(),
                    slack_client=mock_slack,
                )

        assert mock_slack.posted_messages_count >= 1

    async def test_post_mortem_is_valid_model(self, postmortem_state: IncidentState):
        """The post_mortem dict must be parseable as a PostMortem pydantic model."""
        mock_slack = MockSlackClient()
        with postmortem_agent.override(model=FunctionModel(make_postmortem_fn())):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                updates = await postmortem_node(
                    postmortem_state,
                    deps=PostMortemDeps(),
                    slack_client=mock_slack,
                )

        pm = PostMortem.model_validate(updates["post_mortem"])
        assert pm.severity == "P1"
        assert len(pm.action_items) >= 1

    async def test_json_serializable(self, postmortem_state: IncidentState):
        """The post_mortem output must be JSON-serialisable without errors."""
        import json as j

        mock_slack = MockSlackClient()
        with postmortem_agent.override(model=FunctionModel(make_postmortem_fn())):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                updates = await postmortem_node(
                    postmortem_state,
                    deps=PostMortemDeps(),
                    slack_client=mock_slack,
                )

        j.dumps(updates["post_mortem"])  # should not raise

    async def test_persist_called_with_incident_id(self, postmortem_state: IncidentState):
        """_persist_postmortem must be called once with the correct incident_id from state."""
        mock_slack = MockSlackClient()
        with postmortem_agent.override(
            model=FunctionModel(make_postmortem_fn(incident_id="test-001"))
        ):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ) as mock_persist:
                await postmortem_node(
                    postmortem_state,
                    deps=PostMortemDeps(),
                    slack_client=mock_slack,
                )

        mock_persist.assert_called_once()
        call_args = mock_persist.call_args
        # B2 fix: state's incident_id overrides whatever the model returned
        # postmortem_state fixture has incident_id="test-incident-001"
        assert call_args[0][0]["incident_id"] == "test-incident-001"

    async def test_persist_postmortem_with_postgres_url(self):
        """Covers _persist_postmortem lines 58-85 when POSTGRES_URL is set."""
        import os

        from iras.graph.nodes.postmortem import _persist_postmortem

        mock_conn = MagicMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_cursor = MagicMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=None)
        mock_cursor.execute = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()

        pm_dict = {
            "incident_id": "test-001",
            "severity": "P1",
            "root_cause_summary": "DB pool exhaustion",
            "resolution_summary": "Restarted service",
            "timeline": ["10:30 Alert fired"],
            "action_items": ["Add monitoring"],
            "total_duration_minutes": 16,
        }
        state_dict = {
            "started_at": "2024-01-15T10:30:00Z",
            "resolved_at": "2024-01-15T10:46:00Z",
        }

        old = os.environ.pop("POSTGRES_URL", None)
        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=mock_conn)):
                await _persist_postmortem(pm_dict, state_dict)
            mock_cursor.execute.assert_awaited_once()
            mock_conn.commit.assert_awaited_once()
        finally:
            os.environ.pop("POSTGRES_URL", None)
            if old:
                os.environ["POSTGRES_URL"] = old

    async def test_persist_postmortem_db_error_is_logged(self):
        """Covers the except branch in _persist_postmortem."""
        import os

        from iras.graph.nodes.postmortem import _persist_postmortem

        pm_dict = {
            "incident_id": "test-001",
            "severity": "P1",
            "root_cause_summary": "DB pool exhaustion",
            "resolution_summary": "Restarted service",
            "timeline": [],
            "action_items": [],
            "total_duration_minutes": 16,
        }

        old = os.environ.pop("POSTGRES_URL", None)
        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch(
                "psycopg.AsyncConnection.connect",
                AsyncMock(side_effect=RuntimeError("DB failed")),
            ):
                # Should not raise — exception is caught and logged
                await _persist_postmortem(pm_dict, {})
        finally:
            os.environ.pop("POSTGRES_URL", None)
            if old:
                os.environ["POSTGRES_URL"] = old

    async def test_postmortem_node_uses_build_slack_client_when_no_slack_arg(
        self, postmortem_state: IncidentState
    ):
        """Covers lines 167-170 — _build_slack_client() called when slack_client=None."""
        import os

        with postmortem_agent.override(model=FunctionModel(make_postmortem_fn())):
            with patch(
                "iras.graph.nodes.postmortem._persist_postmortem",
                new_callable=AsyncMock,
            ):
                # No SLACK_BOT_TOKEN → MockSlackClient used
                old = os.environ.pop("SLACK_BOT_TOKEN", None)
                try:
                    updates = await postmortem_node(
                        postmortem_state,
                        deps=PostMortemDeps(),
                        # No slack_client argument → triggers _build_slack_client()
                    )
                finally:
                    if old:
                        os.environ["SLACK_BOT_TOKEN"] = old

        assert updates["post_mortem"] is not None

    async def test_build_slack_client_with_token_returns_real_client(self):
        """Covers the SlackClient branch in _build_slack_client (line 170)."""
        import os
        from unittest.mock import patch

        from iras.graph.nodes.postmortem import _build_slack_client

        old = os.environ.pop("SLACK_BOT_TOKEN", None)
        os.environ["SLACK_BOT_TOKEN"] = "xoxb-test-token"
        try:
            with patch("slack_sdk.web.async_client.AsyncWebClient", MagicMock()):
                client = _build_slack_client()
            from iras.tools.slack import SlackClient

            assert isinstance(client, SlackClient)
        finally:
            os.environ.pop("SLACK_BOT_TOKEN", None)
            if old:
                os.environ["SLACK_BOT_TOKEN"] = old
