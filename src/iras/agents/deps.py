"""Dependency injection dataclasses for all Pydantic AI agents.

Each agent has a typed Deps dataclass injected at run-time. Tools are fields on
the dataclass so they can be swapped for mocks in tests without touching agent
logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from iras.tools.deployment import DeploymentClientBase, MockDeploymentClient
from iras.tools.log_fetcher import LogFetcherBase, MockLogFetcher
from iras.tools.metrics import MetricsClientBase, MockMetricsClient
from iras.tools.pagerduty import MockPagerDutyClient, PagerDutyClient
from iras.tools.slack import MockSlackClient, SlackClient


@dataclass
class TriageDeps:
    """Dependencies injected into the triage agent."""

    pass  # Triage operates on the alert payload alone — no external tool calls.


@dataclass
class ContextDeps:
    """Dependencies injected into the context-gathering agent."""

    log_fetcher: LogFetcherBase = field(default_factory=MockLogFetcher)
    metrics_client: MetricsClientBase = field(default_factory=MockMetricsClient)
    deployment_client: DeploymentClientBase = field(default_factory=MockDeploymentClient)


@dataclass
class RCADeps:
    """Dependencies injected into the RCA agent."""

    pass  # RCA reasons over the ContextBundle already in state — no new tool calls.


@dataclass
class RemediationDeps:
    """Dependencies injected into the remediation plan agent."""

    pass  # Plan generation reasons over hypothesis + context — no new tool calls.


@dataclass
class PostMortemDeps:
    """Dependencies injected into the post-mortem agent."""

    pass  # Post-mortem synthesis reasons over the full state — no new tool calls.


@dataclass
class EscalationDeps:
    """Dependencies used by the escalation node (not a Pydantic AI agent)."""

    pagerduty_client: PagerDutyClient | MockPagerDutyClient = field(
        default_factory=MockPagerDutyClient
    )
    slack_client: SlackClient | MockSlackClient = field(default_factory=MockSlackClient)


@dataclass
class ApprovalDeps:
    """Dependencies used by the generate-plan + approval nodes."""

    slack_client: SlackClient | MockSlackClient = field(default_factory=MockSlackClient)
