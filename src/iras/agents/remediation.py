"""Remediation agent — produces a typed remediation plan using Claude Sonnet.

Responsibilities:
- Reason over the root cause hypothesis and context bundle
- Generate an ordered, reversible set of remediation steps
- Ensure every step has a rollback command (enforced by RemediationPlan validator)
- Flag high-risk steps for human approval
- Return a validated RemediationPlan

Model: Claude Sonnet (complex structured output with strict schema requirements)
Retries: 3
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel

from iras.agents.deps import RemediationDeps
from iras.models.incident import RemediationPlan

_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer designing a remediation plan for a production incident.

You will receive the incident triage, context bundle, and root cause hypothesis.

Your task is to generate a safe, reversible remediation plan with these requirements:

1. **Every step MUST have a rollback_command** — a specific command or procedure to undo it.
   Never leave rollback_command empty or set it to "N/A".
   If a step cannot be rolled back, it must be classified as high risk.

2. **Risk classification:**
   - low: Safe to execute, no service impact, fully reversible
   - medium: May cause brief disruption, reversible within 5 minutes
   - high: Significant impact or irreversible; REQUIRES human approval

3. **Steps must be ordered** from safest/fastest to most impactful.

4. **estimated_total_downtime_minutes** should be the expected ADDITIONAL downtime during remediation.

5. **requires_human_approval** must be true if ANY step is high risk.

6. **summary** should be a single paragraph suitable for a Slack message explaining what will happen and why it's safe.

Be conservative. Prefer incremental, observable steps over large atomic changes.
If unsure about impact, classify as high risk.

Respond ONLY with a valid JSON object matching the RemediationPlan schema.
Do NOT include markdown code fences or extra commentary outside the JSON.
"""

remediation_agent = Agent(
    model=AnthropicModel("claude-3-5-sonnet-20241022"),
    output_type=RemediationPlan,
    deps_type=RemediationDeps,
    system_prompt=_SYSTEM_PROMPT,
    retries=3,
)


async def run_remediation_planning(  # pylint: disable=unused-argument
    alert_payload: dict[str, Any],
    triage: dict[str, Any],
    context: dict[str, Any],
    hypothesis: dict[str, Any],
    deps: RemediationDeps | None = None,
) -> RemediationPlan:
    """Run the remediation planning agent.

    Args:
        alert_payload: Raw alert payload from the incident state.
        triage: TriageResult.model_dump() from the incident state.
        context: ContextBundle.model_dump() from the incident state.
        hypothesis: RootCauseHypothesis.model_dump() from the incident state.
        deps: Optional dependency injection.

    Returns:
        A validated RemediationPlan.

    Raises:
        pydantic_ai.exceptions.UnexpectedModelBehavior: If all retries are exhausted.
    """
    if deps is None:
        deps = RemediationDeps()

    user_prompt = (
        "Design a remediation plan for this incident.\n\n"
        f"**Triage:**\n```json\n{json.dumps(triage, indent=2, default=str)}\n```\n\n"
        f"**Context bundle:**\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n\n"
        f"**Root cause hypothesis:**\n```json\n"
        f"{json.dumps(hypothesis, indent=2, default=str)}\n```\n\n"
        "Generate a safe, reversible RemediationPlan. "
        "Every step must have a rollback_command. "
        "Flag high-risk steps honestly."
    )

    result = await remediation_agent.run(user_prompt, deps=deps)
    return result.output
