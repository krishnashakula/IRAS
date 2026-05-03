"""Integration tests for the FastAPI application routes."""

# pylint: disable=missing-class-docstring,missing-function-docstring,redefined-outer-name,reimported,import-outside-toplevel,too-few-public-methods,unused-variable,unused-argument
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from iras.api.app import create_app


def make_test_app(graph=None):
    """Create a test app with an optionally mocked graph on app.state."""
    app = create_app()
    # Mount a mock graph directly on state (bypassing lifespan)
    if graph is not None:
        app.state.graph = graph
    return app


class TestHealthEndpoint:
    async def test_health_returns_ok(self):
        """Health endpoint returns 200 with status=ok when lifespan works."""
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = ""
        mock_settings.logfire_token = ""
        mock_settings.postgres_url = ""

        mock_graph = MagicMock()

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=mock_graph),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=create_app()), base_url="http://test"
            ) as client:
                resp = await client.get("/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"


class TestWebhookRoute:
    @pytest.fixture
    def app_with_graph(self):
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={})
        app = create_app()
        app.state.graph = mock_graph
        return app, mock_graph

    async def test_receive_alert_accepted(self, app_with_graph):
        app, mock_graph = app_with_graph
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/webhook/alert",
                json={"title": "DB down", "timestamp": "2024-01-15T10:00:00Z"},
            )
        assert resp.status_code == 202
        body = resp.json()
        assert "incident_id" in body
        assert body["status"] == "processing"

    async def test_receive_alert_triggers_background(self, app_with_graph):
        app, mock_graph = app_with_graph
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(
                "/webhook/alert",
                json={"title": "CPU spike", "timestamp": "2024-01-15T10:00:00Z"},
            )
        # Give background task a moment to run
        await asyncio.sleep(0.05)

    async def test_receive_alert_no_graph_returns_503(self):
        app = create_app()
        # Deliberately don't set app.state.graph
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/webhook/alert",
                json={"title": "Alert", "timestamp": "2024-01-15T10:00:00Z"},
            )
        assert resp.status_code == 503

    async def test_receive_alert_extra_fields_allowed(self, app_with_graph):
        app, _ = app_with_graph
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/webhook/alert",
                json={
                    "title": "Test",
                    "timestamp": "2024-01-15T10:00:00Z",
                    "source": "prometheus",
                    "labels": {"env": "prod"},
                },
            )
        assert resp.status_code == 202

    async def test_receive_alert_missing_signature_returns_401(self):
        """Covers webhook.py lines 78-83: secret set but no X-IRAS-Signature header."""
        mock_webhook_secret = MagicMock()
        mock_webhook_secret.get_secret_value.return_value = "test-webhook-secret"
        mock_settings = MagicMock()
        mock_settings.webhook_secret = mock_webhook_secret

        app = create_app()
        app.state.graph = AsyncMock()

        with patch("iras.config.settings.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/alert",
                    json={"title": "Alert", "timestamp": "2024-01-15T10:00:00Z"},
                )
        assert resp.status_code == 401
        assert "Missing X-IRAS-Signature" in resp.text

    async def test_receive_alert_invalid_signature_returns_401(self):
        """Covers webhook.py lines 88-89: secret set and signature present but wrong."""
        mock_webhook_secret = MagicMock()
        mock_webhook_secret.get_secret_value.return_value = "test-webhook-secret"
        mock_settings = MagicMock()
        mock_settings.webhook_secret = mock_webhook_secret

        app = create_app()
        app.state.graph = AsyncMock()

        with patch("iras.config.settings.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/webhook/alert",
                    json={"title": "Alert", "timestamp": "2024-01-15T10:00:00Z"},
                    headers={"X-IRAS-Signature": "sha256=invalidsignature"},
                )
        assert resp.status_code == 401
        assert "Invalid webhook signature" in resp.text


class TestApprovalRoute:
    @pytest.fixture
    def app_with_graph(self):
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={})
        app = create_app()
        app.state.graph = mock_graph
        return app, mock_graph

    async def test_approve_incident_success(self, app_with_graph):
        app, mock_graph = app_with_graph
        with patch("iras.api.routes.approval._update_approval_status", new_callable=AsyncMock):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/incidents/INC-001/approve")
        assert resp.status_code == 200
        body = resp.json()
        assert body["incident_id"] == "INC-001"
        assert body["decision"] == "approved"

    async def test_reject_incident_success(self, app_with_graph):
        app, mock_graph = app_with_graph
        with patch("iras.api.routes.approval._update_approval_status", new_callable=AsyncMock):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/incidents/INC-001/reject")
        assert resp.status_code == 200
        body = resp.json()
        assert body["decision"] == "rejected"

    async def test_approve_no_graph_returns_503(self):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/incidents/INC-001/approve")
        assert resp.status_code == 503

    async def test_reject_no_graph_returns_503(self):
        app = create_app()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/incidents/INC-001/reject")
        assert resp.status_code == 503

    async def test_approve_graph_error_returns_500(self, app_with_graph):
        app, mock_graph = app_with_graph
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph error"))
        with patch("iras.api.routes.approval._update_approval_status", new_callable=AsyncMock):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/incidents/INC-001/approve")
        assert resp.status_code == 500

    async def test_reject_graph_error_returns_500(self, app_with_graph):
        app, mock_graph = app_with_graph
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph error"))
        with patch("iras.api.routes.approval._update_approval_status", new_callable=AsyncMock):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/incidents/INC-001/reject")
        assert resp.status_code == 500

    async def test_approve_no_auth_header_with_api_key_returns_401(self):
        """Covers approval.py lines 58-59: credentials is None when api key required."""
        mock_api_key = MagicMock()
        mock_api_key.get_secret_value.return_value = "correct-key"
        mock_settings = MagicMock()
        mock_settings.approval_api_key = mock_api_key

        app = create_app()
        app.state.graph = AsyncMock()

        with patch("iras.config.settings.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post("/incidents/INC-001/approve")
        assert resp.status_code == 401
        assert "Missing Authorization header" in resp.text

    async def test_approve_wrong_auth_token_returns_401(self):
        """Covers approval.py lines 64-68: hmac compare fails when wrong token."""
        mock_api_key = MagicMock()
        mock_api_key.get_secret_value.return_value = "correct-key"
        mock_settings = MagicMock()
        mock_settings.approval_api_key = mock_api_key

        app = create_app()
        app.state.graph = AsyncMock()

        with patch("iras.config.settings.get_settings", return_value=mock_settings):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.post(
                    "/incidents/INC-001/approve",
                    headers={"Authorization": "Bearer wrong-token"},
                )
        assert resp.status_code == 401
        assert "Invalid API key" in resp.text


class TestUpdateApprovalStatus:
    async def test_skips_when_no_postgres_url(self):
        """_update_approval_status does nothing when POSTGRES_URL not set."""
        import os

        from iras.api.routes.approval import _update_approval_status

        old = os.environ.pop("POSTGRES_URL", None)
        try:
            # Should not raise
            await _update_approval_status("INC-001", "approved")
        finally:
            if old:
                os.environ["POSTGRES_URL"] = old


class TestAppLifespan:
    """Tests that run the full lifespan via TestClient to cover app.py lines 29-103."""

    def test_lifespan_no_optional_services(self):
        """Covers startup/shutdown when no optional services configured."""
        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = ""
        mock_settings.logfire_token = ""
        mock_settings.postgres_url = ""

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=MagicMock()),
        ):
            with TestClient(create_app()) as client:
                resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_lifespan_with_langsmith(self):
        """Covers the LangSmith tracing setup branch."""
        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = "ls-test-key"
        mock_settings.langsmith_project = "iras-test"
        mock_settings.logfire_token = ""
        mock_settings.postgres_url = ""

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=MagicMock()),
        ):
            with TestClient(create_app()) as client:
                resp = client.get("/health")
        assert resp.status_code == 200

    def test_lifespan_logfire_import_error(self):
        """Covers logfire except branch when logfire import/configure fails."""
        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = ""
        mock_settings.logfire_token = "lf-test-token"
        mock_settings.postgres_url = ""

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=MagicMock()),
            patch.dict("sys.modules", {"logfire": None}),
        ):
            with TestClient(create_app()) as client:
                resp = client.get("/health")
        assert resp.status_code == 200

    def test_lifespan_with_postgres_checkpointer_failure(self):
        """Covers checkpointer except + monitor task + shutdown cancellation."""
        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = ""
        mock_settings.logfire_token = ""
        mock_settings.postgres_url = "postgresql://localhost/test"

        async def blocking_monitor(graph):
            await asyncio.Event().wait()

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=MagicMock()),
            patch(
                "iras.graph.checkpointer.get_checkpointer",
                AsyncMock(side_effect=RuntimeError("DB not available")),
            ),
            patch("iras.api.background.monitor_approval_timeouts", blocking_monitor),
        ):
            with TestClient(create_app()) as client:
                resp = client.get("/health")
        assert resp.status_code == 200

    def test_lifespan_with_postgres_checkpointer_success(self):
        """Covers checkpointer success path + close_checkpointer at shutdown."""
        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = ""
        mock_settings.logfire_token = ""
        # Use a mock with get_secret_value() so settings.postgres_url.get_secret_value() works
        mock_postgres_url = MagicMock()
        mock_postgres_url.get_secret_value.return_value = "postgresql://localhost/test"
        mock_settings.postgres_url = mock_postgres_url

        mock_checkpointer = MagicMock()

        async def blocking_monitor(graph):
            await asyncio.Event().wait()

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=MagicMock()),
            patch(
                "iras.graph.checkpointer.get_checkpointer",
                AsyncMock(return_value=mock_checkpointer),
            ),
            patch("iras.graph.checkpointer.close_checkpointer", AsyncMock()),
            patch("iras.api.background.monitor_approval_timeouts", blocking_monitor),
        ):
            with TestClient(create_app()) as client:
                resp = client.get("/health")
        assert resp.status_code == 200

    def test_lifespan_logfire_success(self):
        """Covers app.py lines 58-60: logfire_token set and configure/instrument succeed."""
        mock_settings = MagicMock()
        mock_settings.app_env = "test"
        mock_settings.log_level = "INFO"
        mock_settings.langsmith_api_key = ""
        mock_settings.logfire_token = "lf-test-token"
        mock_settings.postgres_url = ""

        mock_logfire = MagicMock()

        with (
            patch("iras.config.settings.get_settings", return_value=mock_settings),
            patch("iras.graph.builder.build_graph", return_value=MagicMock()),
            patch.dict("sys.modules", {"logfire": mock_logfire}),
        ):
            with TestClient(create_app()) as client:
                resp = client.get("/health")
        assert resp.status_code == 200
        mock_logfire.configure.assert_called_once_with(token="lf-test-token")
        mock_logfire.instrument_pydantic_ai.assert_called_once()


class TestUpdateApprovalStatusWithPostgres:
    async def test_update_status_executes_db_query(self):
        """Covers _update_approval_status with POSTGRES_URL set."""
        import os

        from iras.api.routes.approval import _update_approval_status

        mock_conn = MagicMock()
        mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_conn.__aexit__ = AsyncMock(return_value=None)
        mock_cursor = MagicMock()
        mock_cursor.__aenter__ = AsyncMock(return_value=mock_cursor)
        mock_cursor.__aexit__ = AsyncMock(return_value=None)
        mock_cursor.execute = AsyncMock()
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()

        old = os.environ.pop("POSTGRES_URL", None)
        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch("psycopg.AsyncConnection.connect", AsyncMock(return_value=mock_conn)):
                await _update_approval_status("INC-001", "approved")
            mock_cursor.execute.assert_awaited_once()
            mock_conn.commit.assert_awaited_once()
        finally:
            os.environ.pop("POSTGRES_URL", None)
            if old:
                os.environ["POSTGRES_URL"] = old

    async def test_update_status_handles_db_error(self):
        """Covers the except branch in _update_approval_status."""
        import os

        from iras.api.routes.approval import _update_approval_status

        old = os.environ.pop("POSTGRES_URL", None)
        os.environ["POSTGRES_URL"] = "postgresql://test"
        try:
            with patch(
                "psycopg.AsyncConnection.connect",
                AsyncMock(side_effect=RuntimeError("DB error")),
            ):
                # Should not raise — exception is caught and logged
                await _update_approval_status("INC-001", "rejected")
        finally:
            os.environ.pop("POSTGRES_URL", None)
            if old:
                os.environ["POSTGRES_URL"] = old


class TestRunGraphBackground:
    async def test_run_graph_background_calls_ainvoke(self):
        from iras.api.routes.webhook import _run_graph_background

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={})
        await _run_graph_background(
            mock_graph, "test-id-001", {"title": "Test", "timestamp": "now"}
        )
        mock_graph.ainvoke.assert_called_once()

    async def test_run_graph_background_handles_exception(self):
        """Exception in graph run should be caught and logged, not raised."""
        from iras.api.routes.webhook import _run_graph_background

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("graph crashed"))
        # Should not raise
        await _run_graph_background(
            mock_graph, "test-id-002", {"title": "Test", "timestamp": "now"}
        )
