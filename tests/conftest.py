"""Shared pytest fixtures for IRAS tests."""
# pylint: disable=redefined-outer-name
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

# Load .env before any agent/model imports that check ANTHROPIC_API_KEY
from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)

# psycopg async requires SelectorEventLoop on Windows (ProactorEventLoop is not supported)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest

from iras.agents.deps import (
    ApprovalDeps,
    ContextDeps,
    EscalationDeps,
    PostMortemDeps,
    RCADeps,
    RemediationDeps,
    TriageDeps,
)
from iras.graph.state import IncidentState
from iras.models.incident import (
    ContextBundle,
    LogEvidence,
    MetricAnomaly,
    PostMortem,
    RemediationPlan,
    RemediationStep,
    RootCauseHypothesis,
    TriageResult,
)
from iras.tools.deployment import DeploymentEvent, MockDeploymentClient
from iras.tools.log_fetcher import MockLogFetcher, RawLogLine
from iras.tools.metrics import MetricResult, MockMetricsClient
from iras.tools.pagerduty import MockPagerDutyClient
from iras.tools.slack import MockSlackClient

# ── Alert payloads ─────────────────────────────────────────────────────────────


@pytest.fixture
def alert_payload() -> dict[str, Any]:
    """Return a standard P1 high-error-rate alert payload for payment-service."""
    return {
        "title": "High error rate on payment-service",
        "timestamp": "2024-01-15T10:30:00Z",
        "source": "prometheus",
        "service": "payment-service",
        "metric": "http_error_rate",
        "current_value": 0.45,
        "threshold": 0.05,
    }


@pytest.fixture
def p0_alert_payload() -> dict[str, Any]:
    """Return a P0 complete-database-outage alert payload."""
    return {
        "title": "Complete database outage — all reads failing",
        "timestamp": "2024-01-15T03:00:00Z",
        "source": "pagerduty",
        "service": "postgres-primary",
        "metric": "db_connection_errors",
        "current_value": 100,
        "threshold": 0,
    }


# ── Triage result ──────────────────────────────────────────────────────────────


@pytest.fixture
def triage_result() -> TriageResult:
    """Return a P1 triage result for payment-service."""
    return TriageResult(
        severity="P1",
        affected_services=["payment-service", "checkout-service"],
        estimated_user_impact=5000,
        confidence=0.9,
        reasoning="High error rate on payment-service affecting downstream checkout flows.",
    )


@pytest.fixture
def triage_dict(triage_result: TriageResult) -> dict[str, Any]:
    """Return triage_result serialised as a plain dict."""
    return triage_result.model_dump()


# ── Context bundle ─────────────────────────────────────────────────────────────


@pytest.fixture
def log_evidence() -> list[LogEvidence]:
    """Return two sample log evidence entries for payment-service DB errors."""
    return [
        LogEvidence(
            timestamp="2024-01-15T10:29:55Z",
            log_line="ERROR payment-service: connection refused to postgres-primary",
            relevance_score=0.95,
        ),
        LogEvidence(
            timestamp="2024-01-15T10:29:58Z",
            log_line="ERROR payment-service: transaction rollback — db unavailable",
            relevance_score=0.88,
        ),
    ]


@pytest.fixture
def metric_anomalies() -> list[MetricAnomaly]:
    """Return a single http_error_rate anomaly fixture."""
    return [
        MetricAnomaly(
            metric_name="http_error_rate",
            current_value=0.45,
            baseline_value=0.01,
            deviation_percentage=4400.0,
        )
    ]


@pytest.fixture
def context_bundle(
    log_evidence: list[LogEvidence], metric_anomalies: list[MetricAnomaly]
) -> ContextBundle:
    """Return a ContextBundle combining log evidence and metric anomalies."""
    return ContextBundle(
        log_evidence=log_evidence,
        metric_anomalies=metric_anomalies,
        recent_deployments=["payment-service@abc123 by alice — 2024-01-15T10:00:00Z"],
        summary="High error rate on payment-service due to database connectivity issues.",
        failed_tools=[],
    )


@pytest.fixture
def context_dict(context_bundle: ContextBundle) -> dict[str, Any]:
    """Return context_bundle serialised as a plain dict."""
    return context_bundle.model_dump()


# ── RCA hypothesis ─────────────────────────────────────────────────────────────


@pytest.fixture
def high_confidence_hypothesis() -> RootCauseHypothesis:
    """Return a high-confidence (0.88) root cause hypothesis fixture."""
    return RootCauseHypothesis(
        primary_cause=(
            "Database primary node ran out of connections due to connection pool"
            " exhaustion after the payment-service deployment at 10:00 UTC."
        ),
        contributing_factors=[
            "connection pool size not increased for new traffic",
            "missing health check on db pool",
        ],
        evidence=[
            LogEvidence(
                timestamp="2024-01-15T10:29:55Z",
                log_line="ERROR payment-service: connection refused to postgres-primary",
                relevance_score=0.95,
            )
        ],
        confidence=0.88,
        recommended_action="Increase connection pool size from 10 to 50 and restart payment-service",
        alternative_hypotheses=["Network partition between payment-service and postgres-primary"],
    )


@pytest.fixture
def low_confidence_hypothesis() -> RootCauseHypothesis:
    """Return a low-confidence (0.35) root cause hypothesis with no evidence."""
    return RootCauseHypothesis(
        primary_cause="Unclear root cause — insufficient evidence collected.",
        contributing_factors=[],
        evidence=[],
        confidence=0.35,
        recommended_action="Escalate to on-call DBA",
        alternative_hypotheses=[],
    )


@pytest.fixture
def hypothesis_dict(high_confidence_hypothesis: RootCauseHypothesis) -> dict[str, Any]:
    """Return high_confidence_hypothesis serialised as a plain dict."""
    return high_confidence_hypothesis.model_dump()


# ── Remediation plan ───────────────────────────────────────────────────────────


@pytest.fixture
def remediation_plan() -> RemediationPlan:
    """Return a two-step low/medium risk remediation plan that requires no human approval."""
    return RemediationPlan(
        steps=[
            RemediationStep(
                action="Increase payment-service DB connection pool size from 10 to 50",
                rollback_command="kubectl set env deployment/payment-service DB_POOL_SIZE=10",
                risk_level="low",
                estimated_duration_seconds=30,
            ),
            RemediationStep(
                action="Rolling restart payment-service pods",
                rollback_command="kubectl rollout undo deployment/payment-service",
                risk_level="medium",
                estimated_duration_seconds=120,
            ),
        ],
        estimated_total_downtime_minutes=5,
        reversible=True,
        requires_human_approval=False,
        summary="Increase DB pool size and restart payment-service to restore connections.",
    )


@pytest.fixture
def plan_dict(remediation_plan: RemediationPlan) -> dict[str, Any]:
    """Return remediation_plan serialised as a plain dict."""
    return remediation_plan.model_dump()


# ── Post-mortem ────────────────────────────────────────────────────────────────


@pytest.fixture
def post_mortem() -> PostMortem:
    """Return a fully-populated PostMortem fixture for test-incident-001."""
    return PostMortem(
        incident_id="test-incident-001",
        timeline=[
            "10:30 UTC — Alert fired: high error rate on payment-service",
            "10:31 UTC — Triage complete: P1, payment-service + checkout-service",
            "10:35 UTC — RCA complete: DB connection pool exhaustion",
            "10:40 UTC — Remediation plan approved",
            "10:45 UTC — Remediation applied: pool size increased, service restarted",
            "10:46 UTC — Error rate back to baseline",
        ],
        root_cause_summary=(
            "DB connection pool exhaustion after payment-service deployment at 10:00 UTC."
        ),
        resolution_summary=(
            "Increased DB connection pool size from 10 to 50 and performed rolling restart."
        ),
        action_items=[
            "Add automated connection pool scaling to payment-service deployment pipeline",
            "Add DB pool exhaustion alert to observability stack",
            "Review connection pool settings for all services that connect to postgres-primary",
        ],
        severity="P1",
        total_duration_minutes=16,
    )


# ── Mock tool clients ──────────────────────────────────────────────────────────


@pytest.fixture
def mock_log_fetcher() -> MockLogFetcher:
    """Return a MockLogFetcher pre-seeded with payment-service error logs."""
    return MockLogFetcher(
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
    )


@pytest.fixture
def mock_metrics_client() -> MockMetricsClient:
    """Return a MockMetricsClient pre-seeded with an http_error_rate anomaly."""
    return MockMetricsClient(
        responses={
            "http_error_rate": MetricResult(
                metric_name="http_error_rate",
                current_value=0.45,
                baseline_value=0.01,
                deviation_percentage=4400.0,
                is_anomalous=True,
            )
        }
    )


@pytest.fixture
def mock_deployment_client() -> MockDeploymentClient:
    """Return a MockDeploymentClient pre-seeded with a payment-service deployment event."""
    return MockDeploymentClient(
        responses={
            "payment-service": [
                DeploymentEvent(
                    service="payment-service",
                    commit_hash="abc123",
                    author="alice",
                    deployed_at="2024-01-15T10:00:00Z",
                    diff_summary="Increase request timeout from 5s to 30s",
                    environment="production",
                )
            ]
        }
    )


@pytest.fixture
def mock_slack_client() -> MockSlackClient:
    """Return an in-memory MockSlackClient."""
    return MockSlackClient()


@pytest.fixture
def mock_pagerduty_client() -> MockPagerDutyClient:
    """Return an in-memory MockPagerDutyClient."""
    return MockPagerDutyClient()


# ── Deps fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def triage_deps() -> TriageDeps:
    """Return a default TriageDeps instance."""
    return TriageDeps()


@pytest.fixture
def context_deps(
    mock_log_fetcher: MockLogFetcher,
    mock_metrics_client: MockMetricsClient,
    mock_deployment_client: MockDeploymentClient,
) -> ContextDeps:
    """Return a ContextDeps wired to the mock log/metrics/deployment clients."""
    return ContextDeps(
        log_fetcher=mock_log_fetcher,
        metrics_client=mock_metrics_client,
        deployment_client=mock_deployment_client,
    )


@pytest.fixture
def rca_deps() -> RCADeps:
    """Return a default RCADeps instance."""
    return RCADeps()


@pytest.fixture
def remediation_deps() -> RemediationDeps:
    """Return a default RemediationDeps instance."""
    return RemediationDeps()


@pytest.fixture
def postmortem_deps() -> PostMortemDeps:
    """Return a default PostMortemDeps instance."""
    return PostMortemDeps()


@pytest.fixture
def escalation_deps(
    mock_pagerduty_client: MockPagerDutyClient,
    mock_slack_client: MockSlackClient,
) -> EscalationDeps:
    """Return an EscalationDeps wired to mock PagerDuty and Slack clients."""
    return EscalationDeps(
        pagerduty_client=mock_pagerduty_client,
        slack_client=mock_slack_client,
    )


@pytest.fixture
def approval_deps(mock_slack_client: MockSlackClient) -> ApprovalDeps:
    """Return an ApprovalDeps wired to the mock Slack client."""
    return ApprovalDeps(slack_client=mock_slack_client)


# ── Complete IncidentState ─────────────────────────────────────────────────────


@pytest.fixture
def base_incident_state(
    alert_payload: dict[str, Any],
    triage_dict: dict[str, Any],
    context_dict: dict[str, Any],
    hypothesis_dict: dict[str, Any],
    plan_dict: dict[str, Any],
) -> IncidentState:
    """Return a fully-populated IncidentState with all fields set for test-incident-001."""
    return IncidentState(
        alert_payload=alert_payload,
        incident_id="test-incident-001",
        started_at="2024-01-15T10:30:00Z",
        triage=triage_dict,
        context=context_dict,
        hypothesis=hypothesis_dict,
        remediation_plan=plan_dict,
        rca_attempts=1,
        human_approved=True,
        escalated=False,
        post_mortem=None,
        resolved_at=None,
        approval_slack_thread_ts="1705312200.000001",
        error=None,
    )
