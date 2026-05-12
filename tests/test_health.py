"""Smoke test — verifies the FastAPI app boots and /healthz responds."""

from __future__ import annotations

import pytest

from app import __version__


@pytest.mark.unit
def test_healthz_returns_ok(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "version": __version__}


@pytest.mark.unit
def test_openapi_schema_available(client) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Product System"
