"""Deployment tool — fetches recent CI/CD deployment events.

Queries a configurable CI/CD backend for deployments to affected services
within a given lookback window. Used by the context-gathering agent to
identify recent changes that may have caused the incident.
"""  # pylint: disable=missing-class-docstring,missing-function-docstring,too-few-public-methods,too-many-locals,import-outside-toplevel,broad-exception-caught

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Domain types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeploymentEvent:
    service: str
    commit_hash: str
    author: str
    deployed_at: str
    """ISO-8601 timestamp of the deployment."""
    diff_summary: str
    """Short description of what changed, or empty string if unavailable."""
    environment: str = "production"


# ── Base class ────────────────────────────────────────────────────────────────


class DeploymentClientBase(ABC):
    @abstractmethod
    async def fetch_deployments(
        self,
        service: str,
        lookback_hours: int = 24,
    ) -> list[DeploymentEvent]:
        """Return deployment events for *service* within the lookback window."""


# ── GitHub Actions backend ────────────────────────────────────────────────────


class GitHubDeploymentClient(DeploymentClientBase):
    """Queries GitHub Deployments API for recent production deploys."""

    def __init__(self, base_url: str, token: str, org: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._org = org
        self._client = httpx.AsyncClient(
            timeout=10.0,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        self.last_error: str | None = None

    async def fetch_deployments(
        self,
        service: str,
        lookback_hours: int = 24,
    ) -> list[DeploymentEvent]:
        self.last_error = None
        from datetime import datetime, timedelta

        cutoff = datetime.now(tz=UTC) - timedelta(hours=lookback_hours)

        try:
            resp = await self._client.get(
                f"{self._base_url}/repos/{self._org}/{service}/deployments",
                params={"environment": "production", "per_page": 30},
            )
            resp.raise_for_status()
            deployments = resp.json()
            events: list[DeploymentEvent] = []

            for dep in deployments:
                created_at_str = dep.get("created_at", "")
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if created_at < cutoff:
                    continue

                sha = dep.get("sha", "")
                creator = dep.get("creator", {}).get("login", "unknown")

                # Fetch commit details for diff summary
                diff_summary = ""
                try:
                    commit_resp = await self._client.get(
                        f"{self._base_url}/repos/{self._org}/{service}/commits/{sha}"
                    )
                    if commit_resp.status_code == 200:
                        diff_summary = (
                            commit_resp.json().get("commit", {}).get("message", "").split("\n")[0]
                        )
                except Exception:  # noqa: BLE001
                    logger.debug("Failed to fetch commit details for %s@%s", service, sha)

                events.append(
                    DeploymentEvent(
                        service=service,
                        commit_hash=sha[:12],
                        author=creator,
                        deployed_at=created_at_str,
                        diff_summary=diff_summary,
                        environment=dep.get("environment", "production"),
                    )
                )
            return events

        except Exception as exc:
            self.last_error = str(exc)
            logger.warning(
                "GitHub deployment fetch failed",
                extra={"service": service, "error": str(exc)},
            )
            return []

    async def aclose(self) -> None:
        await self._client.aclose()


# ── Mock backend ──────────────────────────────────────────────────────────────


class MockDeploymentClient(DeploymentClientBase):
    """Pre-seeded mock for unit and integration tests."""

    def __init__(
        self,
        responses: dict[str, list[DeploymentEvent]] | None = None,
        *,
        should_fail: bool = False,
    ) -> None:
        self.responses: dict[str, list[DeploymentEvent]] = responses or {}
        self.should_fail = should_fail
        self.calls: list[dict[str, Any]] = []

    async def fetch_deployments(
        self,
        service: str,
        lookback_hours: int = 24,
    ) -> list[DeploymentEvent]:
        self.calls.append({"service": service, "lookback_hours": lookback_hours})
        if self.should_fail:
            return []
        return self.responses.get(service, [])
