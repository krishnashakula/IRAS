"""Approval routes — approve or reject a pending remediation plan.

POST /incidents/{incident_id}/approve
POST /incidents/{incident_id}/reject

Both endpoints resume the paused LangGraph graph with a Command(resume={...})
and update the pending_approvals Postgres table.

Optional Bearer-token authentication:
  When APPROVAL_API_KEY is set, requests must include:
    Authorization: Bearer <key>
  Requests missing or with an invalid key are rejected with 401.
  Leave APPROVAL_API_KEY unset to disable auth (development only).
"""

# pylint: disable=import-outside-toplevel
from __future__ import annotations

import hmac
import logging
import os
from typing import Any  # noqa: I001

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langgraph.types import Command
from pydantic import BaseModel

router = APIRouter(prefix="/incidents", tags=["approval"])
logger = logging.getLogger(__name__)


class ApprovalResponse(BaseModel):
    """Response body returned after an approve or reject action."""

    incident_id: str
    decision: str
    status: str


_bearer = HTTPBearer(auto_error=False)


async def _require_approval_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    """Bearer-token guard for approve/reject endpoints.

    Only enforced when APPROVAL_API_KEY is configured.
    Uses constant-time comparison to prevent timing attacks.
    """
    from iras.config.settings import get_settings

    settings = get_settings()
    if settings.approval_api_key is None:
        return  # Auth disabled — dev / unprotected deployment

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(
        credentials.credentials,
        settings.approval_api_key.get_secret_value(),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _get_graph(request: Request) -> Any:
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graph not initialised",
        )
    return graph


async def _update_approval_status(incident_id: str, decision: str) -> None:
    """Mark the pending_approvals row as decided."""
    postgres_url = os.environ.get("POSTGRES_URL")
    if not postgres_url:
        return
    try:
        import psycopg

        async with await psycopg.AsyncConnection.connect(postgres_url) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE pending_approvals
                    SET status = %s
                    WHERE incident_id = %s AND status = 'pending'
                    """,
                    (decision, incident_id),
                )
            await conn.commit()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning(
            "Failed to update pending_approvals",
            extra={"incident_id": incident_id, "error": str(exc)},
        )


async def _resume_graph(graph: Any, incident_id: str, approved: bool) -> None:
    """Resume the paused graph with the human decision."""
    config = {"configurable": {"thread_id": incident_id}}
    command: Command[Any] = Command(resume={"approved": approved})
    await graph.ainvoke(command, config=config)


@router.post(
    "/{incident_id}/approve",
    response_model=ApprovalResponse,
    summary="Approve the remediation plan for an incident",
)
async def approve_incident(
    incident_id: str,
    request: Request,
    _auth: None = Depends(_require_approval_key),
) -> ApprovalResponse:
    """Resume the paused graph, signalling human approval."""
    graph = _get_graph(request)

    logger.info(
        "Approval received",
        extra={"incident_id": incident_id, "decision": "approved"},
    )

    try:
        await _update_approval_status(incident_id, "approved")
        await _resume_graph(graph, incident_id, approved=True)
    except Exception as exc:
        logger.error(
            "Failed to resume graph after approval",
            extra={"incident_id": incident_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume incident {incident_id}: {exc}",
        ) from exc

    return ApprovalResponse(incident_id=incident_id, decision="approved", status="resumed")


@router.post(
    "/{incident_id}/reject",
    response_model=ApprovalResponse,
    summary="Reject the remediation plan for an incident",
)
async def reject_incident(
    incident_id: str,
    request: Request,
    _auth: None = Depends(_require_approval_key),
) -> ApprovalResponse:
    """Resume the paused graph, signalling human rejection."""
    graph = _get_graph(request)

    logger.info(
        "Rejection received",
        extra={"incident_id": incident_id, "decision": "rejected"},
    )

    try:
        await _update_approval_status(incident_id, "rejected")
        await _resume_graph(graph, incident_id, approved=False)
    except Exception as exc:
        logger.error(
            "Failed to resume graph after rejection",
            extra={"incident_id": incident_id, "error": str(exc)},
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to resume incident {incident_id}: {exc}",
        ) from exc

    return ApprovalResponse(incident_id=incident_id, decision="rejected", status="resumed")
