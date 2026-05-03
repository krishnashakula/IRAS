"""Unit tests for iras.tools.deployment — MockDeploymentClient and domain types."""

from __future__ import annotations

from datetime import UTC

import httpx
import respx

from iras.tools.deployment import (
    DeploymentEvent,
    GitHubDeploymentClient,
    MockDeploymentClient,
)


class TestDeploymentEvent:
    def test_create_event(self):
        event = DeploymentEvent(
            service="api",
            commit_hash="abc123",
            author="alice",
            deployed_at="2024-01-15T10:00:00Z",
            diff_summary="Fix connection pool",
        )
        assert event.service == "api"
        assert event.environment == "production"

    def test_default_environment(self):
        event = DeploymentEvent(
            service="svc",
            commit_hash="xyz",
            author="bob",
            deployed_at="2024-01-15T10:00:00Z",
            diff_summary="",
        )
        assert event.environment == "production"


class TestMockDeploymentClient:
    async def test_returns_empty_by_default(self):
        client = MockDeploymentClient()
        result = await client.fetch_deployments("api")
        assert result == []

    async def test_returns_seeded_responses(self):
        event = DeploymentEvent(
            service="api",
            commit_hash="abc",
            author="alice",
            deployed_at="2024-01-01T00:00:00Z",
            diff_summary="Fix bug",
        )
        client = MockDeploymentClient(responses={"api": [event]})
        result = await client.fetch_deployments("api")
        assert len(result) == 1
        assert result[0].author == "alice"

    async def test_should_fail_returns_empty(self):
        event = DeploymentEvent(
            service="api",
            commit_hash="abc",
            author="alice",
            deployed_at="2024-01-01T00:00:00Z",
            diff_summary="",
        )
        client = MockDeploymentClient(responses={"api": [event]}, should_fail=True)
        result = await client.fetch_deployments("api")
        assert result == []

    async def test_records_calls(self):
        client = MockDeploymentClient()
        await client.fetch_deployments("api", lookback_hours=12)
        assert len(client.calls) == 1
        assert client.calls[0]["service"] == "api"
        assert client.calls[0]["lookback_hours"] == 12

    async def test_unknown_service_returns_empty(self):
        event = DeploymentEvent(
            service="api",
            commit_hash="abc",
            author="alice",
            deployed_at="2024-01-01T00:00:00Z",
            diff_summary="",
        )
        client = MockDeploymentClient(responses={"api": [event]})
        result = await client.fetch_deployments("unknown-service")
        assert result == []


class TestGitHubDeploymentClient:
    @respx.mock
    async def test_fetch_deployments_success(self):
        from datetime import datetime, timedelta

        recent = (
            (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )

        respx.get("https://api.github.com/repos/myorg/api/deployments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "abc123def456",
                        "creator": {"login": "alice"},
                        "created_at": recent,
                        "environment": "production",
                    }
                ],
            )
        )
        respx.get("https://api.github.com/repos/myorg/api/commits/abc123def456").mock(
            return_value=httpx.Response(
                200,
                json={"commit": {"message": "Fix connection pool\nLong description"}},
            )
        )

        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        events = await client.fetch_deployments("api")
        assert len(events) == 1
        assert events[0].author == "alice"
        assert events[0].commit_hash == "abc123def456"
        assert events[0].diff_summary == "Fix connection pool"
        await client._client.aclose()

    @respx.mock
    async def test_fetch_deployments_http_error(self):
        respx.get("https://api.github.com/repos/myorg/api/deployments").mock(
            return_value=httpx.Response(500)
        )
        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        result = await client.fetch_deployments("api")
        assert result == []
        assert client.last_error is not None
        await client._client.aclose()

    @respx.mock
    async def test_fetch_deployments_filters_old_events(self):
        """Deployments older than lookback should be excluded."""
        respx.get("https://api.github.com/repos/myorg/api/deployments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "oldshaaaaaaa",
                        "creator": {"login": "bob"},
                        "created_at": "2020-01-01T00:00:00Z",
                        "environment": "production",
                    }
                ],
            )
        )
        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        events = await client.fetch_deployments("api", lookback_hours=24)
        assert events == []
        await client._client.aclose()

    @respx.mock
    async def test_fetch_deployments_bad_timestamp(self):
        """Deployments with unparseable timestamp should be skipped."""
        respx.get("https://api.github.com/repos/myorg/api/deployments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "abc",
                        "creator": {"login": "bob"},
                        "created_at": "not-a-timestamp",
                        "environment": "production",
                    }
                ],
            )
        )
        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        events = await client.fetch_deployments("api")
        assert events == []
        await client._client.aclose()

    @respx.mock
    async def test_commit_fetch_fails_gracefully(self):
        """Even when commit detail fetch fails, deployment event is still added."""
        from datetime import datetime, timedelta

        recent = (
            (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )

        respx.get("https://api.github.com/repos/myorg/api/deployments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "abc123def456",
                        "creator": {"login": "alice"},
                        "created_at": recent,
                        "environment": "production",
                    }
                ],
            )
        )
        respx.get("https://api.github.com/repos/myorg/api/commits/abc123def456").mock(
            return_value=httpx.Response(404)
        )
        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        events = await client.fetch_deployments("api")
        assert len(events) == 1
        assert events[0].diff_summary == ""
        await client._client.aclose()

    @respx.mock
    async def test_commit_fetch_raises_exception_is_caught(self):
        """Covers lines 108-109 — exception during commit fetch is silently ignored."""
        from datetime import datetime, timedelta

        recent = (
            (datetime.now(tz=UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        )

        respx.get("https://api.github.com/repos/myorg/api/deployments").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {
                        "sha": "commitexcept",
                        "creator": {"login": "bob"},
                        "created_at": recent,
                        "environment": "production",
                    }
                ],
            )
        )
        respx.get("https://api.github.com/repos/myorg/api/commits/commitexcept").mock(
            side_effect=httpx.ConnectError("Network error")
        )
        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        events = await client.fetch_deployments("api")
        assert len(events) == 1
        assert events[0].diff_summary == ""
        await client._client.aclose()

    async def test_aclose_closes_http_client(self):
        """Covers line 132 — aclose() delegates to the underlying httpx client."""
        from unittest.mock import AsyncMock, patch

        client = GitHubDeploymentClient(
            base_url="https://api.github.com",
            token="test-token",
            org="myorg",
        )
        with patch.object(client._client, "aclose", new_callable=AsyncMock) as mock_aclose:
            await client.aclose()
        mock_aclose.assert_awaited_once()
