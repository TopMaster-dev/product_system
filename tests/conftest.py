"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app


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
