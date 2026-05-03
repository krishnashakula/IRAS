"""Unit tests for iras.tools.metrics."""

# pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods,protected-access
from __future__ import annotations

import httpx
import respx

from iras.tools.metrics import (
    MetricResult,
    MockMetricsClient,
    PrometheusMetricsClient,
)


class TestMetricResult:
    def test_create(self):
        result = MetricResult(
            metric_name="http_requests_total",
            current_value=150.0,
            baseline_value=100.0,
            deviation_percentage=50.0,
            is_anomalous=True,
        )
        assert result.metric_name == "http_requests_total"
        assert result.is_anomalous is True


class TestMockMetricsClient:
    async def test_returns_empty_by_default(self):
        client = MockMetricsClient()
        result = await client.query_range(
            ["metric_a"], "2024-01-01T00:00:00Z", "2024-01-01T01:00:00Z"
        )
        assert result == []

    async def test_returns_seeded_responses_filtered_by_name(self):
        m = MetricResult("metric_a", 10.0, 8.0, 25.0, True)
        n = MetricResult("metric_b", 5.0, 5.0, 0.0, False)
        client = MockMetricsClient(responses=[m, n])
        result = await client.query_range(["metric_a"], "a", "b")
        assert len(result) == 1
        assert result[0].metric_name == "metric_a"

    async def test_should_fail_returns_empty(self):
        m = MetricResult("metric_a", 10.0, 8.0, 25.0, True)
        client = MockMetricsClient(responses=[m], should_fail=True)
        result = await client.query_range(["metric_a"], "a", "b")
        assert result == []

    async def test_records_calls(self):
        client = MockMetricsClient()
        await client.query_range(["m1", "m2"], "start", "end")
        assert len(client.calls) == 1
        assert client.calls[0]["metrics"] == ["m1", "m2"]

    async def test_returns_all_matching_metrics(self):
        results = [
            MetricResult("cpu_usage", 90.0, 50.0, 80.0, True),
            MetricResult("mem_usage", 70.0, 65.0, 7.7, False),
            MetricResult("disk_io", 100.0, 80.0, 25.0, True),
        ]
        client = MockMetricsClient(responses=results)
        result = await client.query_range(["cpu_usage", "disk_io"], "a", "b")
        assert len(result) == 2
        names = {r.metric_name for r in result}
        assert names == {"cpu_usage", "disk_io"}


class TestPrometheusMetricsClient:
    @respx.mock
    async def test_instant_query_success(self):
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"result": [{"value": ["1705316400", "150.5"]}]}},
            )
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        val = await client._instant_query("http_requests_total", "2024-01-15T10:00:00Z")
        assert val == 150.5
        await client.aclose()

    @respx.mock
    async def test_instant_query_empty(self):
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json={"data": {"result": []}})
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        val = await client._instant_query("http_requests_total", "2024-01-15T10:00:00Z")
        assert val is None
        await client.aclose()

    @respx.mock
    async def test_instant_query_http_error(self):
        respx.get("http://prometheus:9090/api/v1/query").mock(return_value=httpx.Response(503))
        client = PrometheusMetricsClient("http://prometheus:9090")
        val = await client._instant_query("metric", "time")
        assert val is None
        await client.aclose()

    @respx.mock
    async def test_range_query_success(self):
        respx.get("http://prometheus:9090/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "values": [
                                    ["1705230000", "100.0"],
                                    ["1705233600", "105.0"],
                                    ["1705237200", "98.0"],
                                ]
                            }
                        ]
                    }
                },
            )
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        vals = await client._range_query("metric", "start", "end")
        assert vals == [100.0, 105.0, 98.0]
        await client.aclose()

    @respx.mock
    async def test_range_query_error(self):
        respx.get("http://prometheus:9090/api/v1/query_range").mock(
            return_value=httpx.Response(500)
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        vals = await client._range_query("metric", "start", "end")
        assert vals == []
        await client.aclose()

    @respx.mock
    async def test_query_range_full_flow(self):
        # instant query
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"result": [{"value": ["ts", "150.0"]}]}},
            )
        )
        # range query (baseline)
        respx.get("http://prometheus:9090/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "result": [
                            {
                                "values": [
                                    ["t1", "100.0"],
                                    ["t2", "102.0"],
                                    ["t3", "98.0"],
                                    ["t4", "101.0"],
                                ]
                            }
                        ]
                    }
                },
            )
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        results = await client.query_range(
            ["http_requests_total"],
            "2024-01-15T09:00:00Z",
            "2024-01-15T10:00:00Z",
        )
        assert len(results) == 1
        assert results[0].metric_name == "http_requests_total"
        assert results[0].current_value == 150.0
        await client.aclose()

    async def test_query_range_invalid_timestamps(self):
        client = PrometheusMetricsClient("http://prometheus:9090")
        results = await client.query_range(["metric"], "invalid-ts", "also-invalid")
        assert results == []
        await client.aclose()

    @respx.mock
    async def test_query_range_no_instant_value(self):
        # instant query returns empty
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json={"data": {"result": []}})
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        results = await client.query_range(
            ["metric"], "2024-01-15T09:00:00Z", "2024-01-15T10:00:00Z"
        )
        assert results == []
        await client.aclose()

    @respx.mock
    async def test_query_range_no_baseline_values(self):
        # instant returns value, range returns empty
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"result": [{"value": ["ts", "150.0"]}]}},
            )
        )
        respx.get("http://prometheus:9090/api/v1/query_range").mock(
            return_value=httpx.Response(200, json={"data": {"result": []}})
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        results = await client.query_range(
            ["metric"], "2024-01-15T09:00:00Z", "2024-01-15T10:00:00Z"
        )
        assert results == []
        await client.aclose()

    @respx.mock
    async def test_anomaly_detection_zero_baseline(self):
        """When baseline is exactly 0, deviation_pct should be 0."""
        # instant: 5.0
        respx.get("http://prometheus:9090/api/v1/query").mock(
            return_value=httpx.Response(200, json={"data": {"result": [{"value": ["ts", "5.0"]}]}})
        )
        # range: all zeros
        respx.get("http://prometheus:9090/api/v1/query_range").mock(
            return_value=httpx.Response(
                200,
                json={"data": {"result": [{"values": [["t1", "0.0"], ["t2", "0.0"]]}]}},
            )
        )
        client = PrometheusMetricsClient("http://prometheus:9090")
        results = await client.query_range(
            ["zero_metric"], "2024-01-15T09:00:00Z", "2024-01-15T10:00:00Z"
        )
        assert len(results) == 1
        assert results[0].deviation_percentage == 0.0
        await client.aclose()
