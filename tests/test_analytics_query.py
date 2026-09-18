"""Reading the rollups back over a period.

The defect this file exists to prevent is a stock figure 28 times too large.
`daily_kpi_snapshots` holds flows and balances side by side, and summing a
balance produces a number that is merely big rather than obviously wrong —
nobody reviewing a dashboard catches it, because nobody knows what the stock
figure should be.

The rest is about refusing to invent numbers: a turnover ratio over no stock, a
percentage change from a zero baseline, an unmapped share of an empty period.
Every one of those has a tempting default that would be read as fact.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.services.analytics_query import (
    STALE_AFTER_HOURS,
    UNMAPPED_LABEL,
    Delta,
    Kpis,
    Provenance,
    channel_share,
    period_kpis,
)
from app.services.timeframe import Period

pytestmark = pytest.mark.unit


def _kpis(**kw: Any) -> Kpis:
    base = {
        "gross_sales_jpy": Decimal(0),
        "sold_quantity": 0,
        "order_count": 0,
        "unmapped_sales_jpy": Decimal(0),
        "total_on_hand_qty": 0,
        "out_of_stock_sku_count": 0,
        "sku_count": 0,
        "average_on_hand_qty": 0.0,
        "days_with_data": 0,
    }
    return Kpis(**{**base, **kw})  # type: ignore[arg-type]


# --- turnover -------------------------------------------------------------


def test_turnover_is_sales_over_average_stock() -> None:
    assert _kpis(sold_quantity=300, average_on_hand_qty=100.0).turnover == 3.0


def test_turnover_over_no_stock_is_unanswerable_not_infinite() -> None:
    """A SKU population holding nothing did not turn over infinitely fast. The
    screen renders None as an em dash rather than a number to act on."""
    assert _kpis(sold_quantity=50, average_on_hand_qty=0.0).turnover is None


def test_turnover_of_a_dormant_period_is_zero_not_none() -> None:
    """Stock held and nothing sold IS a turnover of zero, and that is a real
    finding. Only a missing denominator is unanswerable."""
    assert _kpis(sold_quantity=0, average_on_hand_qty=800.0).turnover == 0.0


def test_turnover_is_not_annualised() -> None:
    """A 7-day window reporting 52x its own ratio would be compared against a
    90-day window's and read as a change in the business."""
    week = _kpis(sold_quantity=70, average_on_hand_qty=1000.0)
    assert week.turnover == pytest.approx(0.07)


# --- unmapped share -------------------------------------------------------


def test_the_headline_total_includes_unmapped_revenue() -> None:
    k = _kpis(gross_sales_jpy=Decimal(900), unmapped_sales_jpy=Decimal(100))
    assert k.total_with_unmapped_jpy == Decimal(1000)
    assert k.unmapped_ratio == pytest.approx(0.1)


def test_an_empty_period_has_no_unmapped_share_rather_than_a_division_error() -> None:
    assert _kpis().unmapped_ratio == 0.0
    assert _kpis().unmapped_is_material is False


def test_the_notice_fires_above_five_percent_not_at_it() -> None:
    at = _kpis(gross_sales_jpy=Decimal(95), unmapped_sales_jpy=Decimal(5))
    above = _kpis(gross_sales_jpy=Decimal(94), unmapped_sales_jpy=Decimal(6))
    assert at.unmapped_is_material is False
    assert above.unmapped_is_material is True


# --- deltas ---------------------------------------------------------------


def test_a_delta_from_a_zero_baseline_has_no_percentage() -> None:
    """Growth from nothing is not +100%. Inventing one puts a figure on the
    screen the client would reasonably quote."""
    assert Delta.of(500, 0).change is None


def test_an_ordinary_delta() -> None:
    d = Delta.of(150, 100)
    assert d.change == pytest.approx(50.0)


def test_a_decline_is_negative() -> None:
    assert Delta.of(50, 100).change == pytest.approx(-50.0)


# --- provenance -----------------------------------------------------------


def _prov(**kw: Any) -> Provenance:
    base = {
        "last_rollup_at": None,
        "data_start": None,
        "snapshot_drift_count": None,
        "stale_hours": None,
    }
    return Provenance(**{**base, **kw})  # type: ignore[arg-type]


def test_staleness_is_flagged_at_the_threshold() -> None:
    assert _prov(stale_hours=STALE_AFTER_HOURS).is_stale is True
    assert _prov(stale_hours=STALE_AFTER_HOURS - 0.1).is_stale is False


def test_a_rollup_that_has_never_run_is_not_reported_as_fresh() -> None:
    """stale_hours is None when there is no successful run to measure from.
    Reading that as "not stale" would show a green banner over an empty page."""
    p = _prov()
    assert p.is_stale is False
    assert p.last_rollup_at is None


def test_drift_of_zero_is_not_drift() -> None:
    assert _prov(snapshot_drift_count=0).has_drift is False
    assert _prov(snapshot_drift_count=1).has_drift is True


def test_unmeasured_drift_is_not_reported_as_clean() -> None:
    """The hourly job leaves the column NULL. That means "not checked", and the
    banner must not claim the invariant holds."""
    assert _prov(snapshot_drift_count=None).has_drift is False


# --- the flows/balances distinction ---------------------------------------


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def one(self) -> Any:
        return self._rows[0]

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    def __init__(self, answers: list[list[Any]]) -> None:
        self._answers = answers
        self.scalars: list[Any] = []

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])

    async def scalar(self, _stmt: Any) -> Any:
        return self.scalars.pop(0) if self.scalars else None


PERIOD = Period(date(2026, 9, 1), date(2026, 9, 28), "28d")


async def test_stock_is_read_from_the_closing_day_not_summed() -> None:
    """The whole point of this module. 28 days at 5,000 units is 5,000 units."""
    session = _FakeSession(
        [
            # flows: sales, qty, orders, unmapped, avg_on_hand, days
            [(Decimal(1000), 40, 12, Decimal(0), 5000.0, 28)],
            # closing balances, most recent first
            [(5000, 7, 620), (4900, 9, 620)],
        ]
    )
    k = await period_kpis(session, PERIOD)  # type: ignore[arg-type]
    assert k.total_on_hand_qty == 5000
    assert k.out_of_stock_sku_count == 7
    assert k.sold_quantity == 40  # the flow IS summed


async def test_a_window_with_no_rollup_rows_reports_zero_not_a_crash() -> None:
    """A period before the system started. Every screen offers a 365-day
    preset, and the shop is younger than that."""
    session = _FakeSession([[(Decimal(0), 0, 0, Decimal(0), 0.0, 0)], []])
    k = await period_kpis(session, PERIOD)  # type: ignore[arg-type]
    assert k.total_on_hand_qty == 0
    assert k.days_with_data == 0
    assert k.turnover is None


async def test_averages_divide_by_days_with_data() -> None:
    """A 365-day window over 30 days of history must not read as 1/12th the
    stock. The SQL AVG already skips absent rows; this records that it is the
    intended behaviour rather than an accident."""
    session = _FakeSession([[(Decimal(0), 300, 0, Decimal(0), 100.0, 30)], [(100, 0, 10)]])
    k = await period_kpis(session, PERIOD)  # type: ignore[arg-type]
    assert k.days_with_data == 30
    assert k.turnover == 3.0


# --- channel share --------------------------------------------------------


async def test_unmapped_revenue_appears_as_its_own_row() -> None:
    """Dropping it would make the channel rows sum to less than the headline
    total, and both sit on the same screen."""
    session = _FakeSession([[("rakuten", Decimal(600)), ("shopify", Decimal(300))]])
    session.scalars = [Decimal(100)]
    rows = await channel_share(session, PERIOD)  # type: ignore[arg-type]
    assert rows == [
        ("rakuten", Decimal(600)),
        ("shopify", Decimal(300)),
        (UNMAPPED_LABEL, Decimal(100)),
    ]
    assert sum(v for _, v in rows) == Decimal(1000)


async def test_a_zero_unmapped_total_adds_no_row() -> None:
    """An empty row would read as a defect that needs attention."""
    session = _FakeSession([[("shopify", Decimal(50))]])
    session.scalars = [Decimal(0)]
    rows = await channel_share(session, PERIOD)  # type: ignore[arg-type]
    assert rows == [("shopify", Decimal(50))]


async def test_channels_are_ordered_by_revenue() -> None:
    session = _FakeSession([[("shopify", Decimal(10)), ("rakuten", Decimal(90))]])
    session.scalars = [Decimal(0)]
    rows = await channel_share(session, PERIOD)  # type: ignore[arg-type]
    assert [c for c, _ in rows] == ["rakuten", "shopify"]


def test_the_period_helper_is_inclusive_of_both_ends() -> None:
    assert PERIOD.days == 28
    assert PERIOD.previous() == Period(date(2026, 8, 4), date(2026, 8, 31), "28d")
