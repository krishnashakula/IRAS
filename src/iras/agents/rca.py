"""Root Cause Analysis agent — produces a typed hypothesis using Claude Sonnet.

Responsibilities:
- Reason over the triage result and context bundle
- Identify the most likely primary cause with supporting evidence
- Assign a confidence score (read by LangGraph for routing)
- Return a validated RootCauseHypothesis

The confidence value drives the graph's retry/escalation routing:
- >= threshold (default 0.7): proceed to human approval checkpoint
- < threshold and attempts < max: loop back to context gathering
- < threshold and attempts >= max: route to manual escalation

Model: Claude Sonnet (complex multi-step reasoning required)
Retries: 3
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel

from iras.agents.deps import RCADeps
from iras.models.incident import RootCauseHypothesis

_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer performing root cause analysis on a production incident.

You will receive:
1. The original alert payload
2. The triage result (severity, affected services, user impact)
3. A context bundle containing log evidence, metric anomalies, and recent deployments

Your task:
1. Analyse all evidence systematically
2. Identify the most likely primary root cause
3. List contributing factors that worsened or triggered the incident
4. Select specific log entries as supporting evidence
5. Assign a confidence score between 0.0 and 1.0:
   - 0.9–1.0: Strong evidence, single clear cause
   - 0.7–0.89: Good evidence, cause is likely but some uncertainty
   - 0.5–0.69: Moderate evidence, multiple plausible causes
   - Below 0.5: Insufficient evidence, more context needed
6. Suggest a recommended action
7. List alternative hypotheses if confidence is below 0.8

Be rigorous. If the evidence is inconclusive, give a low confidence score rather
than guessing. A low confidence score triggers additional evidence gathering.

Respond ONLY with a valid JSON object matching the RootCauseHypothesis schema.
Do NOT include markdown code fences or extra commentary outside the JSON.
"""

rca_agent = Agent(
    model=AnthropicModel("claude-3-5-sonnet-20241022"),
    output_type=RootCauseHypothesis,
    deps_type=RCADeps,
    system_prompt=_SYSTEM_PROMPT,
    retries=3,
)


async def run_rca(
    alert_payload: dict[str, Any],
    triage: dict[str, Any],
    context: dict[str, Any],
    deps: RCADeps | None = None,
) -> RootCauseHypothesis:
    """Run the root cause analysis agent.

    Args:
        alert_payload: Raw alert payload from the incident state.
        triage: TriageResult.model_dump() from the incident state.
        context: ContextBundle.model_dump() from the incident state.
        deps: Optional dependency injection.

    Returns:
        A validated RootCauseHypothesis.

    Raises:
        pydantic_ai.exceptions.UnexpectedModelBehavior: If all retries are exhausted.
    """
    if deps is None:
        deps = RCADeps()

    user_prompt = (
        "Perform root cause analysis on this incident.\n\n"
        f"**Alert payload:**\n```json\n{json.dumps(alert_payload, indent=2, default=str)}\n```\n\n"
        f"**Triage result:**\n```json\n{json.dumps(triage, indent=2, default=str)}\n```\n\n"
        f"**Context bundle:**\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n\n"
        "Analyse all evidence and return a RootCauseHypothesis. "
        "Set confidence honestly — it drives whether more evidence is gathered."
    )

    result = await rca_agent.run(user_prompt, deps=deps)
    return result.output
