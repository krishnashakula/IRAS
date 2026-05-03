"""IRAS API routes package."""

from iras.api.routes.approval import router as approval_router
from iras.api.routes.webhook import router as webhook_router

__all__ = ["webhook_router", "approval_router"]
