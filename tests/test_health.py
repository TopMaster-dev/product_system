"""Smoke test — the app boots, and its health check is on a path that can
actually be reached in production.

`/healthz` could never be reached: Google's edge reserves that exact path and
answers it itself, so the request never arrives at the container. The endpoint
returned 200 here and 404 in production for the whole of Phase 1 and 2. The
first assertion below is the one that matters — it pins the health check to a
path measured to pass through the edge.
"""

from __future__ import annotations

import pytest

from app import __version__
from app.api.health import HEALTH_PATH, LEGACY_HEALTH_PATH


@pytest.mark.unit
def test_health_is_served_on_the_reachable_path(client) -> None:
    response = client.get(HEALTH_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


@pytest.mark.unit
def test_the_reachable_path_is_not_the_one_google_reserves(client) -> None:
    """Pins the fix itself. If someone moves the health check back to
    `/healthz`, production loses its health check again and nothing else here
    would notice."""
    assert HEALTH_PATH == "/health"
    assert LEGACY_HEALTH_PATH not in {HEALTH_PATH}


@pytest.mark.unit
def test_the_legacy_path_still_answers_inside_the_container(client) -> None:
    """Kept as an alias so a container-local probe or an old bookmark works."""
    response = client.get(LEGACY_HEALTH_PATH)
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


@pytest.mark.unit
def test_openapi_schema_available(client) -> None:
    response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Product System"


@pytest.mark.unit
def test_only_the_reachable_path_is_documented(client) -> None:
    """The schema should advertise the path that works, not the one that does
    not — otherwise the next person wires a monitor to the broken one."""
    paths = client.get("/openapi.json").json()["paths"]
    assert HEALTH_PATH in paths
    assert LEGACY_HEALTH_PATH not in paths
