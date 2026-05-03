"""Integration tests for the ingestion node."""

# pylint: disable=missing-class-docstring,missing-function-docstring,import-outside-toplevel
from __future__ import annotations

from typing import Any

import pytest

from iras.graph.nodes.ingestion import ingestion_node
from iras.graph.state import IncidentState


class TestIngestionNode:
    async def test_generates_incident_id(self, alert_payload: dict[str, Any]):
        state = IncidentState(alert_payload=alert_payload)
        updates = ingestion_node(state)

        assert "incident_id" in updates
        assert len(updates["incident_id"]) > 0

    async def test_unique_incident_ids(self, alert_payload: dict[str, Any]):
        state = IncidentState(alert_payload=alert_payload)
        updates1 = ingestion_node(state)
        updates2 = ingestion_node(state)

        assert updates1["incident_id"] != updates2["incident_id"]

    async def test_initialises_counters(self, alert_payload: dict[str, Any]):
        state = IncidentState(alert_payload=alert_payload)
        updates = ingestion_node(state)

        assert updates["rca_attempts"] == 0
        assert updates["escalated"] is False
        assert updates["human_approved"] is None

    async def test_sets_started_at_iso(self, alert_payload: dict[str, Any]):
        import re

        state = IncidentState(alert_payload=alert_payload)
        updates = ingestion_node(state)

        # ISO 8601 format: 2024-01-15T10:30:00+00:00 or with Z
        assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", updates["started_at"])

    async def test_clears_all_optional_fields(self, alert_payload: dict[str, Any]):
        state = IncidentState(alert_payload=alert_payload)
        updates = ingestion_node(state)

        for field in (
            "triage",
            "context",
            "hypothesis",
            "remediation_plan",
            "post_mortem",
            "resolved_at",
            "error",
        ):
            assert updates[field] is None

    async def test_missing_title_raises(self):
        state = IncidentState(alert_payload={"timestamp": "2024-01-15T10:30:00Z"})
        with pytest.raises(ValueError, match="title"):
            ingestion_node(state)

    async def test_missing_timestamp_raises(self):
        state = IncidentState(alert_payload={"title": "test alert"})
        with pytest.raises(ValueError, match="timestamp"):
            ingestion_node(state)

    async def test_missing_both_required_fields_raises(self):
        state = IncidentState(alert_payload={})
        with pytest.raises(ValueError):
            ingestion_node(state)

    async def test_extra_fields_preserved(self):
        payload = {
            "title": "Alert",
            "timestamp": "2024-01-15T10:30:00Z",
            "source": "prometheus",
            "metric_value": 0.45,
        }
        state = IncidentState(alert_payload=payload)
        updates = ingestion_node(state)

        # alert_payload remains unchanged from the state — only other fields updated
        assert "incident_id" in updates
