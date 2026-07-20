"""Built-in operation manual — a non-engineer operations guide served as an
admin page (behind the same Basic Auth), so it stays in sync with the UI."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app import __version__
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()


@router.get("/manual")
async def manual(request: Request, operator: OperatorDep) -> Response:
    return templates.TemplateResponse(
        request,
        "manual.html",
        {"operator": operator, "version": __version__},
    )
