"""Post-mortem generator agent — produces a structured PostMortem using Claude Sonnet.

Responsibilities:
- Synthesise the complete incident timeline from the IncidentState
- Summarise root cause and resolution (or escalation reason)
- Generate actionable follow-up items to prevent recurrence
- Return a validated PostMortem written to Postgres

Model: Claude Sonnet (synthesis over multi-step incident data)
Retries: 3
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel

from iras.agents.deps import PostMortemDeps
from iras.models.incident import PostMortem

_SYSTEM_PROMPT = """\
You are an expert Site Reliability Engineer writing a post-mortem for a resolved production incident.

You will receive the complete incident record including:
- The original alert
- Triage result
- Context bundle (logs, metrics, deployments)
- Root cause hypothesis
- Remediation plan (if applied) or escalation reason
- Resolution status and timestamps

Your task:
1. Build a chronological timeline of key events (alert received, triage completed, context gathered, etc.)
2. Write a clear, concise root cause summary (2-3 sentences)
3. Write a resolution summary — what was done or why it was escalated
4. Generate 3–5 specific, actionable follow-up items to prevent recurrence
   - Each item should be assigned to a team or system
   - Avoid vague items like "improve monitoring" — be specific

Format: Return ONLY a valid JSON object matching the PostMortem schema.
Do NOT include markdown code fences or extra commentary outside the JSON.
"""

postmortem_agent = Agent(
    model=AnthropicModel("claude-3-5-sonnet-20241022"),
    output_type=PostMortem,
    deps_type=PostMortemDeps,
    system_prompt=_SYSTEM_PROMPT,
    retries=3,
)


async def run_postmortem(
    incident_state: dict[str, Any],
    deps: PostMortemDeps | None = None,
) -> PostMortem:
    """Run the post-mortem generation agent.

    Args:
        incident_state: The complete IncidentState dict at the end of the graph.
        deps: Optional dependency injection.

    Returns:
        A validated PostMortem.

    Raises:
        pydantic_ai.exceptions.UnexpectedModelBehavior: If all retries are exhausted.
    """
    if deps is None:
        deps = PostMortemDeps()

    user_prompt = (
        "Generate a post-mortem for this incident.\n\n"
        f"**Complete incident record:**\n"
        f"```json\n{json.dumps(incident_state, indent=2, default=str)}\n```\n\n"
        "Build a thorough post-mortem with a timeline, root cause, "
        "resolution, and actionable follow-ups."
    )

    result = await postmortem_agent.run(user_prompt, deps=deps)
    return result.output
