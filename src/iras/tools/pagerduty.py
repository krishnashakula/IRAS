"""PagerDuty tool — creates incidents via the Events API v2.

Idempotent by design: uses the incident_id as the dedup_key so repeated calls
for the same incident never create duplicate pages.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

import httpx

logger = logging.getLogger(__name__)

PAGERDUTY_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"


# ── Domain types ──────────────────────────────────────────────────────────────

PagerDutySeverity = Literal["critical", "error", "warning", "info"]


@dataclass(frozen=True)
class PagerDutyResult:
    ok: bool
    dedup_key: str
    message: str
    error: str = ""


# ── Real PagerDuty client ─────────────────────────────────────────────────────


class PagerDutyClient:
    """Sends incidents to PagerDuty using the Events API v2."""

    def __init__(self, integration_key: str) -> None:
        self._key = integration_key
        self._client = httpx.AsyncClient(timeout=10.0)

    def _map_severity(self, iras_severity: str) -> PagerDutySeverity:
        """Map IRAS severity labels to PagerDuty severity labels."""
        mapping: dict[str, PagerDutySeverity] = {
            "P0": "critical",
            "P1": "error",
            "P2": "warning",
            "P3": "info",
        }
        return mapping.get(iras_severity.upper(), "error")

    async def trigger_incident(
        self,
        incident_id: str,
        severity: str,
        summary: str,
        details: dict[str, Any],
        source: str = "IRAS",
    ) -> PagerDutyResult:
        """Create or de-duplicate a PagerDuty incident.

        Uses incident_id as dedup_key — calling twice with the same incident_id
        is safe and will not create a duplicate alert.
        """
        payload: dict[str, Any] = {
            "routing_key": self._key,
            "event_action": "trigger",
            "dedup_key": incident_id,
            "payload": {
                "summary": summary,
                "severity": self._map_severity(severity),
                "source": source,
                "custom_details": details,
            },
        }
        try:
            resp = await self._client.post(
                PAGERDUTY_EVENTS_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            body = resp.json()
            return PagerDutyResult(
                ok=True,
                dedup_key=body.get("dedup_key", incident_id),
                message=body.get("message", "Event processed"),
            )
        except Exception as exc:
            logger.error(
                "PagerDuty trigger_incident failed",
                extra={"incident_id": incident_id, "error": str(exc)},
            )
            return PagerDutyResult(
                ok=False,
                dedup_key=incident_id,
                message="",
                error=str(exc),
            )

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Mock PagerDuty client ─────────────────────────────────────────────────────


class MockPagerDutyClient:
    """In-memory mock for unit and integration tests."""

    def __init__(self, *, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.incidents: list[dict[str, Any]] = []

    @property
    def trigger_count(self) -> int:
        return len(self.incidents)

    @property
    def incident_ids_triggered(self) -> set[str]:
        return {i["incident_id"] for i in self.incidents}

    async def trigger_incident(
        self,
        incident_id: str,
        severity: str,
        summary: str,
        details: dict[str, Any],
        source: str = "IRAS",
    ) -> PagerDutyResult:
        if self.should_fail:
            return PagerDutyResult(
                ok=False, dedup_key=incident_id, message="", error="Mock failure"
            )
        # Idempotency: don't duplicate if dedup_key already present
        if not any(i["incident_id"] == incident_id for i in self.incidents):
            self.incidents.append(
                {
                    "incident_id": incident_id,
                    "severity": severity,
                    "summary": summary,
                    "details": details,
                }
            )
        return PagerDutyResult(ok=True, dedup_key=incident_id, message="Event processed")
