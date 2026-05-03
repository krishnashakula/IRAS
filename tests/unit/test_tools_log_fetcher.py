"""Unit tests for iras.tools.log_fetcher."""

from __future__ import annotations

import httpx
import pytest
import respx

from iras.tools.log_fetcher import (
    ElasticsearchLogFetcher,
    LokiLogFetcher,
    MockLogFetcher,
    RawLogLine,
    create_log_fetcher,
)


class TestRawLogLine:
    def test_create_with_defaults(self):
        line = RawLogLine(
            timestamp="2024-01-01T10:00:00Z",
            message="Connection refused",
            level="ERROR",
            service="api",
        )
        assert line.extra == {}
        assert line.service == "api"

    def test_create_with_extra(self):
        line = RawLogLine(
            timestamp="2024-01-01T10:00:00Z",
            message="msg",
            level="WARN",
            service="db",
            extra={"trace_id": "abc"},
        )
        assert line.extra["trace_id"] == "abc"


class TestMockLogFetcher:
    async def test_fetch_returns_empty_by_default(self):
        fetcher = MockLogFetcher()
        result = await fetcher.fetch("api", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z")
        assert result == []

    async def test_fetch_returns_seeded_lines(self):
        line = RawLogLine("2024-01-01T10:00:00Z", "Error", "ERROR", "api")
        fetcher = MockLogFetcher(responses={"api": [line]})
        result = await fetcher.fetch("api", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z")
        assert len(result) == 1
        assert result[0].level == "ERROR"

    async def test_should_fail_returns_empty(self):
        line = RawLogLine("2024-01-01T10:00:00Z", "Error", "ERROR", "api")
        fetcher = MockLogFetcher(responses={"api": [line]}, should_fail=True)
        result = await fetcher.fetch("api", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z")
        assert result == []
        assert fetcher.last_error == "Mock failure"

    async def test_records_calls(self):
        fetcher = MockLogFetcher()
        await fetcher.fetch("svc", "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z")
        assert len(fetcher.calls) == 1
        assert fetcher.calls[0]["service"] == "svc"

    async def test_max_lines_respected(self):
        lines = [RawLogLine(f"ts{i}", f"msg{i}", "ERROR", "api") for i in range(10)]
        fetcher = MockLogFetcher(responses={"api": lines})
        result = await fetcher.fetch("api", "a", "b", max_lines=3)
        assert len(result) == 3

    async def test_unknown_service_returns_empty(self):
        line = RawLogLine("ts", "msg", "ERROR", "api")
        fetcher = MockLogFetcher(responses={"api": [line]})
        result = await fetcher.fetch("other-svc", "a", "b")
        assert result == []

    def test_name_property(self):
        fetcher = MockLogFetcher()
        assert fetcher.name == "MockLogFetcher"


class TestCreateLogFetcher:
    def test_creates_elasticsearch_fetcher(self):
        fetcher = create_log_fetcher(elasticsearch_url="http://localhost:9200")
        assert isinstance(fetcher, ElasticsearchLogFetcher)

    def test_creates_loki_fetcher(self):
        fetcher = create_log_fetcher(loki_url="http://localhost:3100")
        assert isinstance(fetcher, LokiLogFetcher)

    def test_elasticsearch_takes_precedence_over_loki(self):
        fetcher = create_log_fetcher(
            elasticsearch_url="http://es:9200",
            loki_url="http://loki:3100",
        )
        assert isinstance(fetcher, ElasticsearchLogFetcher)

    def test_raises_if_no_url(self):
        with pytest.raises(ValueError, match="At least one"):
            create_log_fetcher()


class TestElasticsearchLogFetcher:
    @respx.mock
    async def test_fetch_success(self):
        respx.post("http://localhost:9200/logs-*/_search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "hits": {
                        "hits": [
                            {
                                "_source": {
                                    "@timestamp": "2024-01-15T10:00:00Z",
                                    "message": "DB connection refused",
                                    "level": "ERROR",
                                    "extra_field": "value",
                                }
                            }
                        ]
                    }
                },
            )
        )
        fetcher = ElasticsearchLogFetcher("http://localhost:9200")
        result = await fetcher.fetch("api", "2024-01-15T09:00:00Z", "2024-01-15T11:00:00Z")
        assert len(result) == 1
        assert result[0].message == "DB connection refused"
        assert result[0].level == "ERROR"
        assert result[0].service == "api"
        await fetcher.aclose()

    @respx.mock
    async def test_fetch_http_error(self):
        respx.post("http://localhost:9200/logs-*/_search").mock(return_value=httpx.Response(500))
        fetcher = ElasticsearchLogFetcher("http://localhost:9200")
        result = await fetcher.fetch("api", "a", "b")
        assert result == []
        assert fetcher.last_error is not None
        await fetcher.aclose()

    @respx.mock
    async def test_fetch_empty_hits(self):
        respx.post("http://localhost:9200/logs-*/_search").mock(
            return_value=httpx.Response(200, json={"hits": {"hits": []}})
        )
        fetcher = ElasticsearchLogFetcher("http://localhost:9200")
        result = await fetcher.fetch("api", "a", "b")
        assert result == []
        await fetcher.aclose()


class TestLokiLogFetcher:
    def test_to_ns_conversion(self):
        fetcher = LokiLogFetcher("http://localhost:3100")
        ns = fetcher._to_ns("2024-01-15T10:00:00Z")
        # Should be a numeric string representing nanoseconds
        assert ns.isdigit()
        assert len(ns) > 10  # Nanoseconds are large numbers

    @respx.mock
    async def test_fetch_success(self):
        # Loki returns ns timestamps
        ts_ns = "1705316400000000000"
        respx.get("http://localhost:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "stream": {"service": "api", "level": "error"},
                                "values": [[ts_ns, "DB connection refused"]],
                            }
                        ]
                    }
                },
            )
        )
        fetcher = LokiLogFetcher("http://localhost:3100")
        result = await fetcher.fetch("api", "2024-01-15T09:00:00Z", "2024-01-15T11:00:00Z")
        assert len(result) == 1
        assert result[0].message == "DB connection refused"
        assert result[0].service == "api"
        await fetcher.aclose()

    @respx.mock
    async def test_fetch_http_error(self):
        respx.get("http://localhost:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(503)
        )
        fetcher = LokiLogFetcher("http://localhost:3100")
        result = await fetcher.fetch("api", "2024-01-15T09:00:00Z", "2024-01-15T11:00:00Z")
        assert result == []
        assert fetcher.last_error is not None
        await fetcher.aclose()

    @respx.mock
    async def test_fetch_empty_result(self):
        respx.get("http://localhost:3100/loki/api/v1/query_range").mock(
            return_value=httpx.Response(200, json={"data": {"result": []}})
        )
        fetcher = LokiLogFetcher("http://localhost:3100")
        result = await fetcher.fetch("api", "2024-01-15T09:00:00Z", "2024-01-15T11:00:00Z")
        assert result == []
        await fetcher.aclose()
