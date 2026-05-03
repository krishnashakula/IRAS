"""Webhook route — receives raw alert payloads and starts an IRAS incident.

POST /webhook/alert
  Body: any JSON object (must contain "title" and "timestamp" fields)
  Response: {"incident_id": "...", "status": "processing"}

Optional HMAC signature verification:
  When WEBHOOK_SECRET is set, callers must include:
    X-IRAS-Signature: sha256=<hmac-sha256-hex of raw request body>
  Requests missing or with an invalid signature are rejected with 401.

The endpoint:
  1. Validates the optional HMAC signature
  2. Initialises the IncidentState with the raw payload
  3. Starts the graph in a background task (fire-and-forget)
  4. Returns the incident_id immediately so the caller can poll or subscribe
"""

# pylint: disable=import-outside-toplevel
from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

router = APIRouter(prefix="/webhook", tags=["webhook"])
logger = logging.getLogger(__name__)


class AlertWebhookPayload(BaseModel):
    """Minimum schema for an incoming alert. Extra fields are passed through."""

    title: str
    timestamp: str

    model_config = {"extra": "allow"}


class WebhookResponse(BaseModel):
    """Response body returned immediately after an alert is accepted for processing."""

    incident_id: str
    status: str = "processing"


def _get_graph(request: Request) -> Any:
    """Dependency: retrieve the compiled graph from app state."""
    graph = getattr(request.app.state, "graph", None)
    if graph is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Graph is not initialised",
        )
    return graph


async def _verify_webhook_signature(
    request: Request,
    x_iras_signature: str | None = Header(default=None, alias="X-IRAS-Signature"),
) -> None:
    """HMAC-SHA256 signature guard.

    Only enforced when WEBHOOK_SECRET is configured. The expected header is:
        X-IRAS-Signature: sha256=<hex_digest>
    where the digest covers the raw request body bytes.
    """
    from iras.config.settings import get_settings

    settings = get_settings()
    if settings.webhook_secret is None:
        return  # Verification disabled — dev / unprotected deployment

    secret = settings.webhook_secret.get_secret_value().encode()
    body = await request.body()
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()

    if x_iras_signature is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-IRAS-Signature header",
        )
    # Use constant-time comparison to prevent timing attacks.
    if not hmac.compare_digest(expected, x_iras_signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )


async def _run_graph_background(graph: Any, incident_id: str, payload: dict[str, Any]) -> None:
    """Run the graph for an alert payload in the background.

    Uses the pre-generated incident_id as both the LangGraph thread_id and the
    initial state incident_id so all three identifiers are the same UUID.
    """
    initial_state = {"alert_payload": payload, "incident_id": incident_id}
    config = {"configurable": {"thread_id": incident_id}}

    try:
        await graph.ainvoke(initial_state, config=config)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "Graph run failed in background",
            extra={"incident_id": incident_id, "error": str(exc)},
            exc_info=True,
        )


@router.post(
    "/alert",
    response_model=WebhookResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest an alert and start incident response",
)
async def receive_alert(
    payload: AlertWebhookPayload,
    background_tasks: BackgroundTasks,
    graph: Any = Depends(_get_graph),
    _sig: None = Depends(_verify_webhook_signature),
) -> WebhookResponse:
    """Accept an alert payload and begin the incident response workflow."""
    # Pre-generate a single UUID used as: HTTP response incident_id, LangGraph
    # thread_id, and the canonical incident_id stored in state.
    incident_id = str(uuid.uuid4())

    raw_payload = payload.model_dump()

    logger.info(
        "Alert received",
        extra={
            "incident_id": incident_id,
            "title": payload.title,
            "timestamp": payload.timestamp,
        },
    )

    background_tasks.add_task(_run_graph_background, graph, incident_id, raw_payload)

    return WebhookResponse(incident_id=incident_id, status="processing")
