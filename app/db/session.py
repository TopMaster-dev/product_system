"""Async SQLAlchemy session factory and FastAPI dependency."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    echo=False,
    pool_pre_ping=True,
    # Cloud SQL prod is db-f1-micro (0.6 GB, shared vCPU) with a small
    # max_connections, and Cloud Run can scale to many instances. SQLAlchemy's
    # defaults (pool_size=5, max_overflow=10) mean each instance may hold up to
    # 15 connections, so a modest scale-out exhausts the server and order
    # ingestion starts failing — which surfaces as missing stock, not as a slow
    # page. Cap it explicitly and recycle before Cloud SQL's idle timeout.
    pool_size=3,
    max_overflow=2,
    pool_recycle=1800,
)

async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding an async DB session."""
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
