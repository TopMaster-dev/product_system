"""Shared pytest fixtures.

Notes on async fixture scoping:
- pytest-asyncio runs each test function in its own event loop. asyncpg
  connections are bound to the loop they were created in, so a
  session-scoped async engine breaks with "attached to a different loop".
- We therefore use a session-scoped SYNC fixture to set up / tear down the
  schema once, and a per-test ASYNC engine that lives inside the test's loop.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
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


def _to_sync_url(async_url: str) -> str:
    return async_url.replace("+asyncpg", "+psycopg2")


@pytest.fixture(scope="session")
def _test_database(test_database_url: str) -> Iterator[str]:
    """Create / drop the schema once per session via a SYNC engine.

    Returns the async URL for downstream async engine fixtures.
    """
    sync_url = _to_sync_url(test_database_url)
    engine = create_engine(sync_url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except (OperationalError, SQLAlchemyError) as exc:  # pragma: no cover - env
        engine.dispose()
        pytest.skip(f"Postgres test DB unavailable: {exc}")

    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
    yield test_database_url
    with engine.begin() as conn:
        Base.metadata.drop_all(conn)
    engine.dispose()


@pytest.fixture
async def _test_engine(_test_database: str) -> AsyncIterator[AsyncEngine]:
    """Per-test async engine bound to the current event loop."""
    engine = create_async_engine(_test_database, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def db_session(_test_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """Yield a session inside a transaction that always rolls back."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with _test_engine.connect() as conn:
        trans = await conn.begin()
        async with factory(bind=conn) as session:
            try:
                yield session
            finally:
                await trans.rollback()
