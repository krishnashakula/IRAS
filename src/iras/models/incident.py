"""All Pydantic domain models for IRAS.

Every LLM output is validated against one of these models.
No agent ever returns a raw string — this is the fundamental constraint.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ── Evidence primitives ────────────────────────────────────────────────────────


class LogEvidence(BaseModel):
    """A single log line identified as relevant to an incident."""

    timestamp: str = Field(description="ISO-8601 timestamp of the log entry")
    log_line: str = Field(description="Full text of the log entry")
    relevance_score: float = Field(
        ge=0.0,
        le=1.0,
        description="How relevant this log line is to the incident (0–1)",
    )


class MetricAnomaly(BaseModel):
    """A metric that has deviated significantly from its baseline."""

    metric_name: str = Field(description="Prometheus metric name or label")
    current_value: float = Field(description="Current measured value")
    baseline_value: float = Field(
        description="7-day rolling baseline value for the same time window"
    )
    deviation_percentage: float = Field(
        description="Percentage deviation from baseline (can be negative)"
    )


# ── Triage ────────────────────────────────────────────────────────────────────


class TriageResult(BaseModel):
    """Produced by the triage agent (Claude Haiku).

    Classifies incident severity, identifies affected services, and estimates
    user impact. This drives SLA timers and on-call routing.
    """

    severity: Literal["P0", "P1", "P2", "P3"] = Field(
        description="Incident priority: P0=critical outage, P1=major, P2=minor, P3=informational"
    )
    affected_services: list[str] = Field(
        description="Names of services confirmed or suspected to be affected"
    )
    estimated_user_impact: int = Field(
        ge=0,
        description="Estimated number of users affected by this incident",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence score of this triage classification",
    )
    reasoning: str = Field(description="Short explanation of how the classification was determined")


# ── Context ───────────────────────────────────────────────────────────────────


class ContextBundle(BaseModel):
    """Produced by the context-gathering agent.

    Aggregates logs, metric anomalies, and recent deployments into a single
    evidence package for the RCA agent.
    """

    log_evidence: list[LogEvidence] = Field(
        description="Relevant log lines gathered from the logging infrastructure"
    )
    metric_anomalies: list[MetricAnomaly] = Field(
        description="Metrics that deviated significantly around the alert time"
    )
    recent_deployments: list[str] = Field(
        description="Deployment descriptions for affected services in the last 24 h"
    )
    summary: str = Field(
        description="Synthesised narrative of the evidence gathered, noting any gaps"
    )
    failed_tools: list[str] = Field(
        default_factory=list,
        description="Names of tools that failed or returned empty results",
    )


# ── Root Cause Analysis ───────────────────────────────────────────────────────


class RootCauseHypothesis(BaseModel):
    """Produced by the RCA agent (Claude Sonnet).

    The confidence value is read by LangGraph to decide whether to loop back
    for more context or proceed to the human approval checkpoint.
    """

    primary_cause: str = Field(description="The most likely root cause of the incident")
    contributing_factors: list[str] = Field(
        description="Secondary factors that worsened or triggered the incident"
    )
    evidence: list[LogEvidence] = Field(
        description="Specific log entries that support this hypothesis"
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in this hypothesis (0–1). "
            "Values >= 0.7 proceed to remediation; lower values trigger a retry."
        ),
    )
    recommended_action: str = Field(description="High-level recommended remediation action")
    alternative_hypotheses: list[str] = Field(
        default_factory=list,
        description="Alternative explanations with lower confidence",
    )


# ── Remediation ───────────────────────────────────────────────────────────────


class RemediationStep(BaseModel):
    """A single, atomic step within a remediation plan.

    Every step MUST have a rollback command — this is enforced at the model level.
    """

    action: str = Field(description="Human-readable description of the action to take")
    rollback_command: str = Field(
        min_length=1,
        description="Exact command to undo this step. Must not be empty.",
    )
    risk_level: Literal["low", "medium", "high"] = Field(
        description="Risk classification of this step"
    )
    estimated_duration_seconds: int = Field(
        ge=0,
        description="Estimated time to complete this step in seconds",
    )


class RemediationPlan(BaseModel):
    """Produced by the remediation agent (Claude Sonnet).

    reversible is computed automatically — True only when every step has a
    non-empty rollback_command. requires_human_approval is forced True when any
    step carries high risk.
    """

    steps: list[RemediationStep] = Field(
        min_length=1,
        description="Ordered list of remediation steps to execute",
    )
    estimated_total_downtime_minutes: float = Field(
        ge=0.0,
        description="Estimated additional downtime while remediation runs",
    )
    reversible: bool = Field(description="True only when every step has a valid rollback command")
    requires_human_approval: bool = Field(
        description="True when any step carries high risk or the plan is irreversible"
    )
    summary: str = Field(description="One-paragraph summary suitable for a Slack message")

    @model_validator(mode="after")
    def derive_flags(self) -> RemediationPlan:
        """Enforce reversible and approval flags based on step content."""
        self.reversible = all(bool(step.rollback_command.strip()) for step in self.steps)
        if any(step.risk_level == "high" for step in self.steps) or not self.reversible:
            self.requires_human_approval = True
        return self


# ── Post-Mortem ───────────────────────────────────────────────────────────────


class PostMortem(BaseModel):
    """Produced by the post-mortem generator agent.

    Written to Postgres after every incident, regardless of outcome.
    """

    incident_id: str = Field(description="Unique identifier for this incident")
    timeline: list[str] = Field(description="Chronological list of key events during the incident")
    root_cause_summary: str = Field(
        description="Plain-language summary of the confirmed root cause"
    )
    resolution_summary: str = Field(
        description="What was done to resolve the incident (or why it was escalated)"
    )
    action_items: list[str] = Field(description="Follow-up tasks to prevent recurrence")
    severity: str = Field(description="Severity string, e.g. P0, P1, P2, P3")
    total_duration_minutes: int = Field(
        ge=0,
        description="Total incident duration from alert receipt to resolution in minutes",
    )
