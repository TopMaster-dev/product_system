"""Health check endpoint.

SERVED AT `/health`, NOT `/healthz`.

`/healthz` is unreachable in production and always was. Google's edge reserves
that exact path and answers it itself with a Google-branded 404 page; the
request never arrives at the container. Measured against the live service on
2026-10-19: `/healthz` came back without an `x-cloud-trace-context` header,
while `/health`, `/livez`, `/status` and even `/nonexistent-path-test` all
reached FastAPI and got its own JSON 404.

So the endpoint returned 200 locally, passed its test, and could not be reached
by anything outside the container — which is the worst combination for a health
check, because a monitor pointed at it reports a permanent outage while an
unmonitored service looks fine. `/healthz` is kept as an alias purely so a
container-local probe or an old bookmark still works.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app import __version__

router = APIRouter(tags=["health"])

#: The path to point any external uptime monitor at.
HEALTH_PATH = "/health"
#: Reachable only from inside the container; see the module docstring.
LEGACY_HEALTH_PATH = "/healthz"


class HealthResponse(BaseModel):
    status: str
    version: str


@router.get(HEALTH_PATH, response_model=HealthResponse)
@router.get(LEGACY_HEALTH_PATH, response_model=HealthResponse, include_in_schema=False)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)
