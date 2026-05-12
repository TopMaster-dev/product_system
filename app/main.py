"""FastAPI application entrypoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router as api_router
from app.config import get_settings
from app.logging import configure_logging, get_logger


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.app_log_level)
    log = get_logger(__name__)
    log.info("app.startup", env=settings.app_env, queue=settings.task_queue_backend)
    yield
    log.info("app.shutdown")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Product System",
        version="0.1.0",
        description="Rakuten x Shopify inventory aggregation (Phase 1-A)",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    return app


app = create_app()
