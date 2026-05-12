"""Shared pytest fixtures."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.main import app
from app.models import Base

TEST_DB_URL_DEFAULT = "postgresql+asyncpg://postgres:postgres@localhost:5432/product_system_test"


@pytest.fixture
def client() -> Iterator[TestClient]:
    """Synchronous FastAPI TestClient."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    """Async HTTP client bound to the FastAPI ASGI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return os.getenv("TEST_DATABASE_URL", TEST_DB_URL_DEFAULT)


@pytest.fixture(scope="session")
async def _test_engine(test_database_url: str):
    """Spin up an engine for the test DB; skip session if Postgres is unreachable."""
    engine = create_async_engine(test_database_url, pool_pre_ping=True, future=True)
    try:
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environmental
        await engine.dispose()
        pytest.skip(f"Postgres test DB unavailable: {exc}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def db_session(_test_engine) -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction that always rolls back."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with _test_engine.connect() as conn:
        trans = await conn.begin()
        async with factory(bind=conn) as session:
            try:
                yield session
            finally:
                await trans.rollback()
