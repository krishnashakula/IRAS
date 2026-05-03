"""IRAS FastAPI application.

Lifespan:
  startup  → initialise settings, configure LangSmith + Logfire, build Postgres
             checkpointer, compile graph, start approval timeout monitor
  shutdown → cancel background task, close checkpointer

Health endpoint:
  GET /health  →  {"status": "ok", "env": "production"}
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before any agent/model imports so ANTHROPIC_API_KEY is in os.environ
load_dotenv(override=False)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # pylint: disable=redefined-outer-name
    """Application lifespan — startup + shutdown."""
    # ── Settings ──────────────────────────────────────────────────────────────
    from iras.config.settings import get_settings

    settings = get_settings()

    # ── Logging ───────────────────────────────────────────────────────────────
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # ── LangSmith ─────────────────────────────────────────────────────────────
    if settings.langsmith_api_key:
        os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
        os.environ.setdefault("LANGCHAIN_API_KEY", settings.langsmith_api_key)
        os.environ.setdefault("LANGCHAIN_PROJECT", settings.langsmith_project)
        logger.info("LangSmith tracing enabled", extra={"project": settings.langsmith_project})

    # ── Logfire ───────────────────────────────────────────────────────────────
    if settings.logfire_token:
        try:
            import logfire

            logfire.configure(token=settings.logfire_token)
            logfire.instrument_pydantic_ai()
            logger.info("Logfire instrumentation enabled")
        except Exception as exc:  # noqa: BLE001  # pylint: disable=broad-exception-caught
            logger.warning("Logfire initialisation failed", extra={"error": str(exc)})

    # ── Checkpointer + Graph ──────────────────────────────────────────────────
    from iras.graph.builder import build_graph
    from iras.graph.checkpointer import close_checkpointer, get_checkpointer

    checkpointer = None
    if settings.postgres_url:
        try:
            checkpointer = await get_checkpointer(settings.postgres_url.get_secret_value())
            logger.info("Postgres checkpointer ready")
        except Exception as exc:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Postgres checkpointer failed — running without persistence",
                extra={"error": str(exc)},
            )

    graph = build_graph(checkpointer=checkpointer)
    app.state.graph = graph
    logger.info("IRAS graph compiled and ready")

    # ── Approval Timeout Monitor ──────────────────────────────────────────────
    from iras.api.background import monitor_approval_timeouts

    timeout_task: asyncio.Task[None] | None = None
    if settings.postgres_url:
        timeout_task = asyncio.create_task(
            monitor_approval_timeouts(graph),
            name="approval_timeout_monitor",
        )
        logger.info("Approval timeout monitor started")

    # ── Yield (application runs) ──────────────────────────────────────────────
    yield

    # ── Shutdown ──────────────────────────────────────────────────────────────
    logger.info("Shutting down IRAS")

    if timeout_task and not timeout_task.done():
        timeout_task.cancel()
        try:
            await timeout_task
        except asyncio.CancelledError:
            pass

    if checkpointer is not None:
        await close_checkpointer()

    logger.info("IRAS shutdown complete")


def create_app() -> FastAPI:
    """Construct the FastAPI application."""
    from iras.api.routes.approval import router as approval_router
    from iras.api.routes.webhook import router as webhook_router

    app = FastAPI(  # pylint: disable=redefined-outer-name
        title="IRAS — Incident Response Agent System",
        description="Autonomous incident response powered by LangGraph + Pydantic AI",
        version="1.0.0",
        lifespan=lifespan,
    )

    from iras.config.settings import get_settings as _get_settings
    _settings = _get_settings()
    _origins = (
        [o.strip() for o in _settings.cors_allowed_origins.split(",") if o.strip()]
        if _settings.cors_allowed_origins != "*"
        else ["*"]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "Authorization", "X-IRAS-Signature"],
        allow_credentials=_settings.cors_allowed_origins != "*",
    )

    app.include_router(webhook_router)
    app.include_router(approval_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        from iras.config.settings import get_settings

        settings = get_settings()
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()
