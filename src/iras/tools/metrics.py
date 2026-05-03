"""Metrics tool — queries Prometheus for anomalous metric values.

Returns current value, 7-day baseline, and deviation percentage for each
requested metric. Flags metrics deviating more than two standard deviations.
"""

from __future__ import annotations

import logging
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Domain types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricResult:
    metric_name: str
    current_value: float
    baseline_value: float
    deviation_percentage: float
    is_anomalous: bool
    """True when deviation exceeds 2 standard deviations from baseline."""


# ── Base class ────────────────────────────────────────────────────────────────


class MetricsClientBase(ABC):
    @abstractmethod
    async def query_range(
        self,
        metric_names: list[str],
        start_ts: str,
        end_ts: str,
    ) -> list[MetricResult]:
        """Return current + baseline metrics with deviation flags."""


# ── Prometheus backend ────────────────────────────────────────────────────────


class PrometheusMetricsClient(MetricsClientBase):
    """Queries Prometheus HTTP API for instant values and 7-day baselines."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=15.0)
        self.last_error: str | None = None

    async def _instant_query(self, query: str, time: str) -> float | None:
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v1/query",
                params={"query": query, "time": time},
            )
            resp.raise_for_status()
            result = resp.json().get("data", {}).get("result", [])
            if result:
                return float(result[0]["value"][1])
            return None
        except Exception as exc:
            logger.warning(
                "Prometheus instant query failed",
                extra={"query": query, "error": str(exc)},
            )
            return None

    async def _range_query(self, query: str, start: str, end: str, step: str = "5m") -> list[float]:
        try:
            resp = await self._client.get(
                f"{self._base_url}/api/v1/query_range",
                params={"query": query, "start": start, "end": end, "step": step},
            )
            resp.raise_for_status()
            result = resp.json().get("data", {}).get("result", [])
            values: list[float] = []
            for series in result:
                values.extend(float(v[1]) for v in series.get("values", []))
            return values
        except Exception as exc:
            logger.warning(
                "Prometheus range query failed",
                extra={"query": query, "error": str(exc)},
            )
            return []

    async def query_range(
        self,
        metric_names: list[str],
        start_ts: str,
        end_ts: str,
    ) -> list[MetricResult]:
        from datetime import datetime, timedelta

        results: list[MetricResult] = []

        # Compute baseline window: same duration, 7 days ago
        try:
            start_dt = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
        except ValueError:
            logger.error("Invalid timestamp format", extra={"start": start_ts, "end": end_ts})
            return results

        baseline_start = (start_dt - timedelta(days=7)).isoformat()
        baseline_end = (end_dt - timedelta(days=7)).isoformat()

        for metric in metric_names:
            current = await self._instant_query(metric, end_ts)
            if current is None:
                continue

            baseline_values = await self._range_query(metric, baseline_start, baseline_end)
            if not baseline_values:
                continue

            baseline = statistics.mean(baseline_values)
            stdev = statistics.stdev(baseline_values) if len(baseline_values) > 1 else 0.0

            if baseline != 0:
                deviation_pct = ((current - baseline) / abs(baseline)) * 100
            else:
                deviation_pct = 0.0

            threshold = 2.0 * stdev if stdev > 0 else abs(baseline) * 0.2
            is_anomalous = abs(current - baseline) > threshold

            results.append(
                MetricResult(
                    metric_name=metric,
                    current_value=current,
                    baseline_value=baseline,
                    deviation_percentage=round(deviation_pct, 2),
                    is_anomalous=is_anomalous,
                )
            )
        return results

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Mock backend ──────────────────────────────────────────────────────────────


class MockMetricsClient(MetricsClientBase):
    """Pre-seeded mock for unit and integration tests."""

    def __init__(
        self,
        responses: list[MetricResult] | None = None,
        *,
        should_fail: bool = False,
    ) -> None:
        self.responses: list[MetricResult] = responses or []
        self.should_fail = should_fail
        self.calls: list[dict[str, Any]] = []

    async def query_range(
        self,
        metric_names: list[str],
        start_ts: str,
        end_ts: str,
    ) -> list[MetricResult]:
        self.calls.append({"metrics": metric_names, "start": start_ts, "end": end_ts})
        if self.should_fail:
            return []
        return [r for r in self.responses if r.metric_name in metric_names]
