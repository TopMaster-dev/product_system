"""Shared dependencies for the admin UI."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates

from app.services.timeframe import JST

_TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def humanize_status(value: str) -> str:
    return value.replace("_", " ").title()


def jst(value: Any) -> Any:
    """A database timestamp, in Japan time.

    Timestamps are stored in UTC and the screens printed them as they came,
    unlabelled, so an order placed at 12:51 showed in the event log as 03:51 —
    found 2026-10-02 while writing the client's test guide, which points them
    at exactly such an event. Dates pass through untouched (a stocktake date is
    already a calendar day), and so does a naive datetime, which has no zone to
    convert from.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(JST)
    return value


templates.env.filters["humanize_status"] = humanize_status
templates.env.filters["jst"] = jst
