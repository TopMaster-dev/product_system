"""Shared dependencies for the admin UI."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def humanize_status(value: str) -> str:
    return value.replace("_", " ").title()


templates.env.filters["humanize_status"] = humanize_status
