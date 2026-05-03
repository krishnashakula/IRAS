"""Log fetcher tool — queries Elasticsearch or Loki for incident-relevant logs.

Supports two real backends (Elasticsearch, Loki) and one mock backend for
testing. All backends implement the LogFetcherProtocol so they are swappable
without changing agent logic.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Domain types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RawLogLine:
    timestamp: str
    message: str
    level: str
    service: str
    extra: dict[str, Any] = field(default_factory=dict)


# ── Protocol / base ───────────────────────────────────────────────────────────


class LogFetcherBase(ABC):
    """Abstract base for all log fetcher backends."""

    @abstractmethod
    async def fetch(
        self,
        service: str,
        start_ts: str,
        end_ts: str,
        *,
        max_lines: int = 200,
    ) -> list[RawLogLine]:
        """Return log lines for *service* between *start_ts* and *end_ts*.

        Both timestamps are ISO-8601 strings.  Only ERROR and WARNING severity
        lines are returned.  On backend failure, returns an empty list and
        sets self.last_error.
        """

    @property
    def name(self) -> str:
        return self.__class__.__name__


# ── Elasticsearch backend ─────────────────────────────────────────────────────


class ElasticsearchLogFetcher(LogFetcherBase):
    """Queries an Elasticsearch/OpenSearch cluster for structured logs."""

    def __init__(self, base_url: str, index_pattern: str = "logs-*") -> None:
        self._base_url = base_url.rstrip("/")
        self._index = index_pattern
        self._client = httpx.AsyncClient(timeout=10.0)
        self.last_error: str | None = None

    async def fetch(
        self,
        service: str,
        start_ts: str,
        end_ts: str,
        *,
        max_lines: int = 200,
    ) -> list[RawLogLine]:
        self.last_error = None
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"service.keyword": service}},
                        {"terms": {"level.keyword": ["ERROR", "WARNING", "WARN"]}},
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start_ts,
                                    "lte": end_ts,
                                }
                            }
                        },
                    ]
                }
            },
            "sort": [{"@timestamp": {"order": "asc"}}],
            "size": max_lines,
        }
        try:
            resp = await self._client.post(
                f"{self._base_url}/{self._index}/_search",
                json=query,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("hits", [])
            return [
                RawLogLine(
                    timestamp=h["_source"].get("@timestamp", ""),
                    message=h["_source"].get("message", ""),
                    level=h["_source"].get("level", ""),
                    service=service,
                    extra={
                        k: v
                        for k, v in h["_source"].items()
                        if k not in {"@timestamp", "message", "level"}
                    },
                )
                for h in hits
            ]
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning(
                "Elasticsearch log fetch failed",
                extra={"service": service, "error": str(exc)},
            )
            return []

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Loki backend ──────────────────────────────────────────────────────────────


class LokiLogFetcher(LogFetcherBase):
    """Queries a Grafana Loki instance using the HTTP API."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=10.0)
        self.last_error: str | None = None

    def _to_ns(self, iso_ts: str) -> str:
        """Convert ISO-8601 timestamp to Unix nanoseconds string."""
        from datetime import datetime

        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return str(int(dt.replace(tzinfo=UTC).timestamp() * 1e9))

    async def fetch(
        self,
        service: str,
        start_ts: str,
        end_ts: str,
        *,
        max_lines: int = 200,
    ) -> list[RawLogLine]:
        self.last_error = None
        query = f'{{service="{service}"}} |= "" | level=~"(error|warning|warn|ERROR|WARNING|WARN)"'
        params = {
            "query": query,
            "start": self._to_ns(start_ts),
            "end": self._to_ns(end_ts),
            "limit": str(max_lines),
            "direction": "forward",
        }
        try:
            resp = await self._client.get(
                f"{self._base_url}/loki/api/v1/query_range",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            lines: list[RawLogLine] = []
            for stream in data.get("data", {}).get("result", []):
                labels = stream.get("stream", {})
                for ts_ns, log_line in stream.get("values", []):
                    ts_s = str(int(ts_ns) // int(1e9))
                    from datetime import datetime

                    dt = datetime.fromtimestamp(int(ts_s), tz=UTC)
                    lines.append(
                        RawLogLine(
                            timestamp=dt.isoformat(),
                            message=log_line,
                            level=labels.get("level", ""),
                            service=service,
                            extra=labels,
                        )
                    )
            return lines
        except Exception as exc:
            self.last_error = str(exc)
            logger.warning(
                "Loki log fetch failed",
                extra={"service": service, "error": str(exc)},
            )
            return []

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Mock backend ──────────────────────────────────────────────────────────────


class MockLogFetcher(LogFetcherBase):
    """In-memory mock for unit and integration tests.

    Pre-populate ``responses`` to control what the fetcher returns per service.
    Set ``should_fail`` to True to simulate a backend failure (returns empty).
    """

    def __init__(
        self,
        responses: dict[str, list[RawLogLine]] | None = None,
        *,
        should_fail: bool = False,
    ) -> None:
        self.responses: dict[str, list[RawLogLine]] = responses or {}
        self.should_fail = should_fail
        self.calls: list[dict[str, Any]] = []
        self.last_error: str | None = None

    async def fetch(
        self,
        service: str,
        start_ts: str,
        end_ts: str,
        *,
        max_lines: int = 200,
    ) -> list[RawLogLine]:
        self.calls.append({"service": service, "start": start_ts, "end": end_ts})
        if self.should_fail:
            self.last_error = "Mock failure"
            return []
        return self.responses.get(service, [])[:max_lines]


# ── Factory ───────────────────────────────────────────────────────────────────


def create_log_fetcher(
    elasticsearch_url: str | None = None,
    loki_url: str | None = None,
) -> LogFetcherBase:
    """Return the appropriate backend based on configuration."""
    if elasticsearch_url:
        return ElasticsearchLogFetcher(elasticsearch_url)
    if loki_url:
        return LokiLogFetcher(loki_url)
    raise ValueError("At least one of elasticsearch_url or loki_url must be provided")
