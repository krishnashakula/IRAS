"""Triage agent — classifies incident severity using Claude Haiku.

Responsibilities:
- Parse the raw alert payload
- Assign P0/P1/P2/P3 severity
- Identify affected services
- Estimate user impact
- Return a validated TriageResult

Model: Claude Haiku (fast, cost-efficient — triage does not require deep reasoning)
Retries: 3 (Pydantic AI handles validation retries automatically)
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel

from iras.agents.deps import TriageDeps as TriageDeps
from iras.models.incident import TriageResult

_SYSTEM_PROMPT = """You are an expert Site Reliability Engineer performing incident triage.

Given a raw production alert payload, you must:
1. Classify the severity using this rubric:
   - P0: Complete service outage, >10k users affected, or data loss risk
   - P1: Major degradation, 1k-10k users affected, or SLA breach imminent
   - P2: Partial degradation, <1k users affected, or single service impaired
   - P3: Informational, no user impact, or warning threshold crossed

2. Identify all affected services mentioned in the alert.
3. Estimate the number of users affected (use 0 if unknown).
4. Provide a confidence score between 0.0 and 1.0.
5. Explain your reasoning briefly.

Respond ONLY with a valid JSON object matching the TriageResult schema.
Do NOT include markdown code fences, extra commentary, or explanations outside the JSON.
"""

triage_agent = Agent(
    model=AnthropicModel("claude-3-haiku-20240307"),
    output_type=TriageResult,
    deps_type=TriageDeps,
    system_prompt=_SYSTEM_PROMPT,
    retries=3,
)


async def run_triage(alert_payload: dict[str, Any], deps: TriageDeps | None = None) -> TriageResult:
    """Run the triage agent against a raw alert payload.

    Args:
        alert_payload: The raw webhook payload dict.
        deps: Optional dependency injection (defaults to empty TriageDeps).

    Returns:
        A validated TriageResult.

    Raises:
        pydantic_ai.exceptions.UnexpectedModelBehavior: If all 3 retries are exhausted.
    """
    if deps is None:
        deps = TriageDeps()

    user_prompt = (
        "Triage the following production alert:\n\n"
        f"```json\n{json.dumps(alert_payload, indent=2, default=str)}\n```"
    )
    result = await triage_agent.run(user_prompt, deps=deps)
    return result.output
