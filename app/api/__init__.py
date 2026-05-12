"""HTTP API layer — FastAPI routers."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.health import router as health_router

router = APIRouter()
router.include_router(health_router)

__all__ = ["router"]
