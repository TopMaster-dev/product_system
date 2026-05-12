"""HTTP API layer — FastAPI routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.health import router as health_router
from app.api.webhooks import router as webhooks_router
from app.ui import router as admin_router

router = APIRouter()
router.include_router(health_router)
router.include_router(webhooks_router)
router.include_router(admin_router)

__all__ = ["router"]
