"""画面の時刻は日本時間で表示する.

Timestamps are stored in UTC, and every admin screen printed them as stored,
without a label: an order at 12:51 appeared in the event log at 03:51. Found on
2026-10-02 while writing the client's test guide, which sends them to exactly
that event to check how shared stock is decremented.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.ui.deps import jst

pytestmark = pytest.mark.unit

TEMPLATES = Path("app/ui/templates")


def test_a_utc_timestamp_is_shown_in_japan_time() -> None:
    shown = jst(datetime(2026, 10, 2, 3, 51, tzinfo=UTC))
    assert shown.strftime("%Y-%m-%d %H:%M") == "2026-10-02 12:51"


def test_the_day_moves_when_utc_is_still_on_the_previous_one() -> None:
    """20:00 UTC is 05:00 the next morning in Japan — the case where a date
    printed from UTC is not merely an hour off but on the wrong day."""
    assert jst(datetime(2026, 10, 1, 20, 0, tzinfo=UTC)).date() == date(2026, 10, 2)


def test_a_date_passes_through() -> None:
    """A stocktake date is a calendar day already; there is no zone to apply."""
    assert jst(date(2026, 10, 2)) == date(2026, 10, 2)


def test_a_naive_datetime_is_left_alone() -> None:
    """Without a zone, converting would silently assume the server's."""
    naive = datetime(2026, 10, 2, 3, 51)
    assert jst(naive) is naive


def test_no_template_prints_a_stored_timestamp_without_converting_it() -> None:
    """Every `*_at` column is a stored UTC timestamp. Printing one directly is
    the defect; `(x|jst).strftime(...)` is the only accepted form."""
    raw = re.compile(r"\b[a-z_]+\.[a-z_]+_at\.strftime\(")
    offenders = [
        f"{p.name}: {m.group(0)}"
        for p in sorted(TEMPLATES.glob("*.html"))
        for m in raw.finditer(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"timestamps printed in UTC: {offenders}"


def test_the_guard_reads_real_templates() -> None:
    converted = sum(p.read_text(encoding="utf-8").count("|jst)") for p in TEMPLATES.glob("*.html"))
    assert converted >= 10, "the templates no longer convert timestamps — the guard is vacuous"
