"""Unit tests for iras.tools.pagerduty — MockPagerDutyClient and PagerDutyClient."""

# pylint: disable=missing-class-docstring,missing-function-docstring,protected-access,import-outside-toplevel
from __future__ import annotations

import httpx
import respx

from iras.tools.pagerduty import (
    MockPagerDutyClient,
    PagerDutyClient,
)


class TestMockPagerDutyClient:
    async def test_trigger_returns_ok(self):
        client = MockPagerDutyClient()
        result = await client.trigger_incident(
            incident_id="INC-001",
            severity="P0",
            summary="DB down",
            details={"service": "api"},
        )
        assert result.ok is True
        assert result.dedup_key == "INC-001"

    async def test_trigger_stores_incident(self):
        client = MockPagerDutyClient()
        await client.trigger_incident(
            incident_id="INC-001",
            severity="P0",
            summary="DB down",
            details={"service": "api"},
        )
        assert client.trigger_count == 1
        assert "INC-001" in client.incident_ids_triggered

    async def test_idempotent_trigger(self):
        """Calling twice with same incident_id should not duplicate."""
        client = MockPagerDutyClient()
        await client.trigger_incident("INC-001", "P0", "DB down", {})
        await client.trigger_incident("INC-001", "P0", "DB down", {})
        assert client.trigger_count == 1

    async def test_multiple_different_incidents(self):
        client = MockPagerDutyClient()
        await client.trigger_incident("INC-001", "P0", "first", {})
        await client.trigger_incident("INC-002", "P1", "second", {})
        assert client.trigger_count == 2
        assert client.incident_ids_triggered == {"INC-001", "INC-002"}

    async def test_should_fail_returns_error(self):
        client = MockPagerDutyClient(should_fail=True)
        result = await client.trigger_incident("INC-001", "P0", "fail", {})
        assert result.ok is False
        assert result.error != ""

    async def test_should_fail_does_not_store_incident(self):
        client = MockPagerDutyClient(should_fail=True)
        await client.trigger_incident("INC-001", "P0", "fail", {})
        assert client.trigger_count == 0

    async def test_trigger_with_source(self):
        client = MockPagerDutyClient()
        result = await client.trigger_incident(
            incident_id="INC-001",
            severity="P1",
            summary="Test",
            details={},
            source="CustomSource",
        )
        assert result.ok is True

    def test_empty_client_trigger_count(self):
        client = MockPagerDutyClient()
        assert client.trigger_count == 0
        assert client.incident_ids_triggered == set()


class TestPagerDutyClientSeverityMapping:
    def test_map_severity_p0(self):
        client = PagerDutyClient("fake-key")
        assert client._map_severity("P0") == "critical"

    def test_map_severity_p1(self):
        client = PagerDutyClient("fake-key")
        assert client._map_severity("P1") == "error"

    def test_map_severity_p2(self):
        client = PagerDutyClient("fake-key")
        assert client._map_severity("P2") == "warning"

    def test_map_severity_p3(self):
        client = PagerDutyClient("fake-key")
        assert client._map_severity("P3") == "info"

    def test_map_severity_unknown_defaults_to_error(self):
        client = PagerDutyClient("fake-key")
        assert client._map_severity("UNKNOWN") == "error"

    def test_map_severity_case_insensitive(self):
        client = PagerDutyClient("fake-key")
        assert client._map_severity("p0") == "critical"


class TestPagerDutyClientTriggerIncident:
    """Tests for PagerDutyClient.trigger_incident (lines 65-94, 102)."""

    @respx.mock
    async def test_trigger_incident_success(self):
        """Covers lines 65-86 — successful PagerDuty trigger."""
        from iras.tools.pagerduty import PAGERDUTY_EVENTS_URL

        respx.post(PAGERDUTY_EVENTS_URL).mock(
            return_value=httpx.Response(
                202,
                json={"dedup_key": "INC-001", "message": "Event processed"},
            )
        )
        client = PagerDutyClient("test-integration-key")
        result = await client.trigger_incident(
            incident_id="INC-001",
            severity="P0",
            summary="DB down",
            details={"service": "postgres"},
        )
        assert result.ok is True
        assert result.dedup_key == "INC-001"
        assert result.message == "Event processed"
        await client.aclose()

    @respx.mock
    async def test_trigger_incident_http_error(self):
        """Covers lines 87-94 — HTTP error returns ok=False."""
        from iras.tools.pagerduty import PAGERDUTY_EVENTS_URL

        respx.post(PAGERDUTY_EVENTS_URL).mock(
            return_value=httpx.Response(400, json={"message": "Bad request"})
        )
        client = PagerDutyClient("test-integration-key")
        result = await client.trigger_incident(
            incident_id="INC-002",
            severity="P1",
            summary="Service degraded",
            details={},
        )
        assert result.ok is False
        assert result.dedup_key == "INC-002"
        await client.aclose()

    @respx.mock
    async def test_trigger_incident_network_error(self):
        """Covers the except Exception branch in trigger_incident."""
        from iras.tools.pagerduty import PAGERDUTY_EVENTS_URL

        respx.post(PAGERDUTY_EVENTS_URL).mock(side_effect=httpx.ConnectError("Connection refused"))
        client = PagerDutyClient("test-integration-key")
        result = await client.trigger_incident(
            incident_id="INC-003",
            severity="P2",
            summary="Network issue",
            details={},
        )
        assert result.ok is False
        assert "Connection refused" in result.error
        await client.aclose()
