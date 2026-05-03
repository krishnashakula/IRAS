"""Unit tests for the context-gathering agent."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from iras.agents.context_gathering import context_agent, run_context_gathering
from iras.agents.deps import ContextDeps
from iras.models.incident import ContextBundle
from iras.tools.deployment import MockDeploymentClient
from iras.tools.log_fetcher import MockLogFetcher, RawLogLine
from iras.tools.metrics import MetricResult, MockMetricsClient


def _make_context_response(failed_tools: list[str] | None = None):
    payload = {
        "log_evidence": [
            {
                "timestamp": "2024-01-15T10:29:55Z",
                "log_line": "ERROR: connection refused",
                "relevance_score": 0.92,
            }
        ],
        "metric_anomalies": [
            {
                "metric_name": "http_error_rate",
                "current_value": 0.45,
                "baseline_value": 0.01,
                "deviation_percentage": 4400.0,
            }
        ],
        "recent_deployments": ["payment-service@abc123"],
        "summary": "High error rate on payment-service; DB connectivity issues suspected.",
        "failed_tools": failed_tools or [],
    }
    return ModelResponse(parts=[TextPart(content=json.dumps(payload))])


def happy_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_context_response()


def with_failed_tools_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return _make_context_response(failed_tools=["fetch_metrics"])


class TestContextGatheringAgent:
    async def test_happy_path(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_deps: ContextDeps,
    ):
        with context_agent.override(model=FunctionModel(happy_model)):
            result = await run_context_gathering(alert_payload, triage_dict, deps=context_deps)

        assert isinstance(result, ContextBundle)
        assert len(result.log_evidence) >= 1
        assert result.failed_tools == []

    async def test_failed_tools_included(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_deps: ContextDeps,
    ):
        with context_agent.override(model=FunctionModel(with_failed_tools_model)):
            result = await run_context_gathering(alert_payload, triage_dict, deps=context_deps)

        assert "fetch_metrics" in result.failed_tools

    async def test_no_log_evidence_still_valid(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_deps: ContextDeps,
    ):
        def empty_logs_model(messages, info):
            payload = {
                "log_evidence": [],
                "metric_anomalies": [],
                "recent_deployments": [],
                "summary": "No evidence found.",
                "failed_tools": ["fetch_logs", "fetch_metrics", "fetch_deployments"],
            }
            return ModelResponse(parts=[TextPart(content=json.dumps(payload))])

        with context_agent.override(model=FunctionModel(empty_logs_model)):
            result = await run_context_gathering(alert_payload, triage_dict, deps=context_deps)

        assert result.log_evidence == []
        assert len(result.failed_tools) == 3

    async def test_model_dump_round_trip(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_deps: ContextDeps,
    ):
        with context_agent.override(model=FunctionModel(happy_model)):
            result = await run_context_gathering(alert_payload, triage_dict, deps=context_deps)

        restored = ContextBundle.model_validate(result.model_dump())
        assert len(restored.log_evidence) == len(result.log_evidence)
        assert restored.failed_tools == result.failed_tools

    async def test_failing_log_fetcher_handled(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
    ):
        """Agent should still return a ContextBundle when the log fetcher fails."""
        failing_deps = ContextDeps(
            log_fetcher=MockLogFetcher(responses={}, should_fail=True),
            metrics_client=MockMetricsClient(responses={}),
            deployment_client=MockDeploymentClient(responses={}),
        )
        with context_agent.override(model=FunctionModel(with_failed_tools_model)):
            result = await run_context_gathering(alert_payload, triage_dict, deps=failing_deps)

        assert isinstance(result, ContextBundle)

    async def test_retry_exhausted_raises(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
        context_deps: ContextDeps,
    ):
        from pydantic_ai.exceptions import UnexpectedModelBehavior

        call_count = 0

        def bad_model(messages, info):
            nonlocal call_count
            call_count += 1
            return ModelResponse(parts=[TextPart(content="INVALID")])

        with context_agent.override(model=FunctionModel(bad_model)):
            with pytest.raises(UnexpectedModelBehavior):
                await run_context_gathering(alert_payload, triage_dict, deps=context_deps)

        assert call_count >= 2


class TestContextGatheringToolBodies:
    """Tests that trigger actual tool function execution (lines 77-78, 101-102, 132-133)."""

    async def test_tools_are_called_via_tool_call_parts(
        self,
        alert_payload: dict[str, Any],
        triage_dict: dict[str, Any],
    ):
        """Use a stateful FunctionModel to trigger all three tool functions."""
        from iras.agents.deps import ContextDeps
        from iras.tools.deployment import MockDeploymentClient
        from iras.tools.log_fetcher import MockLogFetcher
        from iras.tools.metrics import MockMetricsClient

        context_deps = ContextDeps(
            log_fetcher=MockLogFetcher(
                responses={
                    "payment-service": [
                        RawLogLine(
                            timestamp="2024-01-15T10:29:55Z",
                            message="connection refused to postgres-primary",
                            level="ERROR",
                            service="payment-service",
                        )
                    ]
                }
            ),
            metrics_client=MockMetricsClient(
                responses=[
                    MetricResult(
                        metric_name="http_error_rate",
                        current_value=0.45,
                        baseline_value=0.01,
                        deviation_percentage=4400.0,
                        is_anomalous=True,
                    )
                ]
            ),
            deployment_client=MockDeploymentClient(responses={}),
        )
        from pydantic_ai.messages import ToolCallPart

        call_count = [0]

        def tool_calling_model(messages: list, info) -> ModelResponse:
            call_count[0] += 1
            if call_count[0] == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "fetch_logs",
                            json.dumps(
                                {
                                    "service": "payment-service",
                                    "start_ts": "2024-01-15T10:00:00Z",
                                    "end_ts": "2024-01-15T11:00:00Z",
                                }
                            ),
                            "call-1",
                        )
                    ]
                )
            elif call_count[0] == 2:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "fetch_metrics",
                            json.dumps(
                                {
                                    "metric_names": ["http_error_rate"],
                                    "start_ts": "2024-01-15T10:00:00Z",
                                    "end_ts": "2024-01-15T11:00:00Z",
                                }
                            ),
                            "call-2",
                        )
                    ]
                )
            elif call_count[0] == 3:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "fetch_deployments",
                            json.dumps(
                                {
                                    "service": "payment-service",
                                    "lookback_hours": 24,
                                }
                            ),
                            "call-3",
                        )
                    ]
                )
            else:
                return _make_context_response()

        with context_agent.override(model=FunctionModel(tool_calling_model)):
            result = await run_context_gathering(alert_payload, triage_dict, deps=context_deps)

        assert isinstance(result, ContextBundle)
        # Verify all 4 calls were made (3 tool calls + 1 final response)
        assert call_count[0] >= 4
