"""Admin UI — FastAPI + Jinja2 + Tailwind.

Mounts at /admin. All routes require Basic Auth (Phase 1-A) — the
authenticated username is captured as `operator` and recorded on
manual adjustments and mapping resolutions for audit.
"""

from fastapi import APIRouter

from app.ui.routes.adjust import router as adjust_router
from app.ui.routes.alerts import router as alerts_router
from app.ui.routes.events import router as events_router
from app.ui.routes.home import router as home_router
from app.ui.routes.inventory import router as inventory_router
from app.ui.routes.mappings import router as mappings_router
from app.ui.routes.reconcile import router as reconcile_router
from app.ui.routes.sync_errors import router as sync_errors_router

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(home_router)
router.include_router(inventory_router)
router.include_router(reconcile_router)
router.include_router(sync_errors_router)
router.include_router(mappings_router)
router.include_router(adjust_router)
router.include_router(events_router)
router.include_router(alerts_router)

__all__ = ["router"]
