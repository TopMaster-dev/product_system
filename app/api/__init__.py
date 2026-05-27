"""HTTP API layer — FastAPI routers."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from app.api.health import router as health_router
from app.api.internal_jobs import router as internal_jobs_router
from app.api.webhooks import router as webhooks_router
from app.ui import router as admin_router

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """Send bare hostname visitors to the admin UI."""
    return RedirectResponse(url="/admin/", status_code=307)


router.include_router(health_router)
router.include_router(webhooks_router)
router.include_router(internal_jobs_router)
router.include_router(admin_router)

__all__ = ["router"]
