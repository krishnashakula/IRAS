"""Context-gathering agent — collects logs, metrics, and deployment data.

Responsibilities:
- Query the logging infrastructure for affected services in the alert window
- Query Prometheus for anomalous metric values around the alert timestamp
- Check CI/CD for recent deployments to affected services
- Synthesise gathered data into a ContextBundle

The agent uses three injected tools:
- fetch_logs: queries the log backend
- fetch_metrics: queries Prometheus
- fetch_deployments: queries the CI/CD system

Partial tool failures are handled gracefully — the agent notes failed tools in
the ContextBundle summary rather than raising an exception.

Model: Claude Haiku (tool-use workload, cost-sensitive)
Retries: 3
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.anthropic import AnthropicModel

from iras.agents.deps import ContextDeps as ContextDeps
from iras.models.incident import ContextBundle

_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer gathering evidence for an ongoing production incident.

You have three tools at your disposal:
- fetch_logs: retrieves error/warning log lines for a service in a time window
- fetch_metrics: retrieves current vs baseline metric values for a list of metrics
- fetch_deployments: retrieves recent deployments for a service

Your job:
1. Use fetch_logs to retrieve relevant logs for EACH affected service.
2. Use fetch_metrics to check key metrics (error_rate, latency_p99, request_rate, cpu_usage) for affected services.
3. Use fetch_deployments to check if recent deployments may have caused this incident.
4. Synthesise all findings into a ContextBundle.

If any tool returns an empty result, note the failure in the summary and continue.
Always call all three tools before synthesising.

Respond ONLY with a valid JSON object matching the ContextBundle schema.
Do NOT include markdown code fences or extra commentary outside the JSON.
"""

context_agent = Agent(
    model=AnthropicModel("claude-3-haiku-20240307"),
    output_type=ContextBundle,
    deps_type=ContextDeps,
    system_prompt=_SYSTEM_PROMPT,
    retries=3,
)


@context_agent.tool
async def fetch_logs(
    ctx: RunContext[ContextDeps],
    service: str,
    start_ts: str,
    end_ts: str,
) -> str:
    """Fetch error and warning log lines for *service* between *start_ts* and *end_ts*.

    Args:
        service: Service name as it appears in the logging system.
        start_ts: ISO-8601 start timestamp.
        end_ts: ISO-8601 end timestamp.

    Returns:
        JSON string containing a list of log line objects.
    """
    lines = await ctx.deps.log_fetcher.fetch(service, start_ts, end_ts)
    return json.dumps(
        [{"timestamp": log.timestamp, "message": log.message, "level": log.level} for log in lines],
        default=str,
    )


@context_agent.tool
async def fetch_metrics(
    ctx: RunContext[ContextDeps],
    metric_names: list[str],
    start_ts: str,
    end_ts: str,
) -> str:
    """Fetch current and baseline values for *metric_names* in the given time window.

    Args:
        metric_names: List of Prometheus metric names or labels.
        start_ts: ISO-8601 start timestamp.
        end_ts: ISO-8601 end timestamp.

    Returns:
        JSON string containing a list of metric result objects.
    """
    results = await ctx.deps.metrics_client.query_range(metric_names, start_ts, end_ts)
    return json.dumps(
        [
            {
                "metric_name": r.metric_name,
                "current_value": r.current_value,
                "baseline_value": r.baseline_value,
                "deviation_percentage": r.deviation_percentage,
                "is_anomalous": r.is_anomalous,
            }
            for r in results
        ],
        default=str,
    )


@context_agent.tool
async def fetch_deployments(
    ctx: RunContext[ContextDeps],
    service: str,
    lookback_hours: int,
) -> str:
    """Fetch recent deployment events for *service* in the lookback window.

    Args:
        service: Service name as it appears in the CI/CD system.
        lookback_hours: Number of hours to look back (typically 24).

    Returns:
        JSON string containing a list of deployment event objects.
    """
    events = await ctx.deps.deployment_client.fetch_deployments(service, lookback_hours)
    return json.dumps(
        [
            {
                "service": e.service,
                "commit_hash": e.commit_hash,
                "author": e.author,
                "deployed_at": e.deployed_at,
                "diff_summary": e.diff_summary,
                "environment": e.environment,
            }
            for e in events
        ],
        default=str,
    )


async def run_context_gathering(
    alert_payload: dict[str, Any],
    triage: dict[str, Any],
    deps: ContextDeps | None = None,
) -> ContextBundle:
    """Run the context-gathering agent.

    Args:
        alert_payload: Raw alert payload from the incident state.
        triage: TriageResult.model_dump() from the incident state.
        deps: Dependency injection with tool backends.

    Returns:
        A validated ContextBundle.
    """
    if deps is None:
        deps = ContextDeps()

    affected_services = triage.get("affected_services", [])
    alert_time = alert_payload.get("timestamp", "unknown")

    user_prompt = (
        "Gather context for this incident.\n\n"
        f"**Alert payload:**\n```json\n{json.dumps(alert_payload, indent=2, default=str)}\n```\n\n"
        f"**Triage result:**\n```json\n{json.dumps(triage, indent=2, default=str)}\n```\n\n"
        f"**Affected services:** {', '.join(affected_services)}\n"
        f"**Alert timestamp:** {alert_time}\n\n"
        "Use the available tools to gather logs, metrics, and deployments, "
        "then return a synthesised ContextBundle."
    )

    result = await context_agent.run(user_prompt, deps=deps)
    return result.output
