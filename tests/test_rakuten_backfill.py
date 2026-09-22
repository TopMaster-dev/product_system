"""楽天受注の遡及取込 — chunking, and why the boundaries have to abut.

Recovering an ingestion outage is a one-shot operation against an API that will
not tell you what it left out. Two ways to lose orders while appearing to
succeed, both guarded here:

**A gap between chunks.** If one span ends at 23:59:59 and the next starts at
00:00:00 of the following day, anything placed inside that second is in neither
request. Nothing reports it; the totals simply come out low. Spans are built
from whole JST days that abut exactly.

**A truncated page.** `_search_order_numbers` requests page 1 of at most 1000
and never asks for page 2. A chunk returning a full page has almost certainly
lost the rest, and the call succeeds either way — so a full page is reported as
suspect rather than accepted.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest

from app.cli.backfill_rakuten_orders import (
    DEFAULT_CHUNK_DAYS,
    PAGE_LIMIT,
    _jst_bounds,
    chunks,
)

pytestmark = pytest.mark.unit

OUTAGE_START = date(2026, 8, 25)
OUTAGE_END = date(2026, 9, 22)


def test_the_whole_period_is_covered_exactly_once() -> None:
    spans = chunks(OUTAGE_START, OUTAGE_END, DEFAULT_CHUNK_DAYS)
    covered = [d for first, last in spans for d in _days(first, last)]
    assert covered == _days(OUTAGE_START, OUTAGE_END)
    assert len(covered) == len(set(covered))


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def test_consecutive_chunks_abut_with_no_gap() -> None:
    """A day skipped between chunks is an order lost with no error."""
    spans = chunks(OUTAGE_START, OUTAGE_END, 3)
    for (_, end), (start, _) in pairwise(spans):
        assert start == end + timedelta(days=1)


def test_chunk_boundaries_touch_to_the_microsecond() -> None:
    """The UTC instants handed to RMS must meet exactly. A one-second gap
    between 23:59:59 and the next 00:00:00 drops whatever fell in it."""
    _, first_end = _jst_bounds(date(2026, 8, 25))
    second_start, _ = _jst_bounds(date(2026, 8, 26))
    assert first_end == second_start


def test_a_jst_day_starts_at_1500_utc_the_day_before() -> None:
    """JST is UTC+9, so a JST calendar day begins at 15:00 UTC on the previous
    date. Building the window in UTC would shift every boundary nine hours and
    split each day's orders across two chunks."""
    start, end = _jst_bounds(date(2026, 8, 25))
    assert start.isoformat() == "2026-08-24T15:00:00+00:00"
    assert end.isoformat() == "2026-08-25T15:00:00+00:00"


def test_a_single_day_period_is_one_chunk() -> None:
    assert chunks(OUTAGE_START, OUTAGE_START, 3) == [(OUTAGE_START, OUTAGE_START)]


def test_the_last_chunk_is_clipped_to_the_end_date() -> None:
    """A final chunk running past `until` would search into the live period and
    re-ingest orders the scheduler already has. Harmless — ingest is idempotent
    — but it makes the reported totals meaningless as a measure of the gap."""
    spans = chunks(date(2026, 9, 1), date(2026, 9, 5), 3)
    assert spans[-1] == (date(2026, 9, 4), date(2026, 9, 5))


def test_chunk_days_of_one_gives_a_span_per_day() -> None:
    spans = chunks(date(2026, 9, 1), date(2026, 9, 3), 1)
    assert spans == [
        (date(2026, 9, 1), date(2026, 9, 1)),
        (date(2026, 9, 2), date(2026, 9, 2)),
        (date(2026, 9, 3), date(2026, 9, 3)),
    ]


def test_the_outage_splits_into_a_manageable_number_of_calls() -> None:
    """29 days at the default. Small enough to watch, few enough not to spend
    an afternoon on Rakuten's rate limit."""
    spans = chunks(OUTAGE_START, OUTAGE_END, DEFAULT_CHUNK_DAYS)
    assert 5 <= len(spans) <= 15


def test_the_page_limit_matches_what_the_adapter_requests() -> None:
    """If the adapter's requestRecordsAmount ever changes, this constant has to
    move with it — otherwise a truncated chunk stops being detected."""
    from app.adapters import rakuten

    source = rakuten.__file__
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    assert f'"requestRecordsAmount": {PAGE_LIMIT}' in body
