"""Bulk re-resolution — and the one flag that decides whether stock moves.

`apply_stock` has no safe default, so it is required. Getting it wrong is
silent in both directions:

  * False on a fresh backlog leaves real sales unrecorded against stock.
  * True on the historical backlog subtracts goods the physical count already
    accounted for — every SKU in the backlog quietly drops by its own sales
    history, months after the fact.

The spec for W7-5 settles it for the historical pass: 歴史的明細には
inventory_events を発行しない. `reresolve_order_items` therefore passes False,
and the test below is what stops that turning back into True.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.models import OrderStatusEnum
from app.services.mapping import reresolve_unmapped_lines

pytestmark = pytest.mark.unit


class _Order:
    def __init__(self, oid: int, status: str = OrderStatusEnum.PENDING_MAPPING) -> None:
        self.id = oid
        self.channel = "rakuten"
        self.channel_order_id = f"R-{oid}"
        self.marketplace_id = None
        self.status = status
        self.ordered_at = datetime(2026, 3, 4, tzinfo=UTC)


class _Item:
    def __init__(self, sku: str, quantity: int = 1, line_id: str = "L-1") -> None:
        self.channel_sku = sku
        self.quantity = quantity
        self.line_id = line_id
        self.unit_price = Decimal(0)
        self.master_sku_id: int | None = None


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Result:
        return self

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    """Answers the scan, then one mapping lookup per line, then the settle."""

    def __init__(self, scan: list[Any], lookups: list[Any], still_unmapped: list[int]) -> None:
        self._queue: list[_Result] = [_Result(scan)]
        self._queue += [_Result([v] if v is not None else []) for v in lookups]
        self._queue.append(_Result(still_unmapped))
        self.flushes = 0

    async def execute(self, _stmt: Any) -> _Result:
        return self._queue.pop(0) if self._queue else _Result([])

    async def flush(self) -> None:
        self.flushes += 1


@pytest.fixture(autouse=True)
def _no_real_inventory(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Records what would have been consumed, instead of touching a database."""
    calls: list[tuple[int, int]] = []

    class _Recorder:
        def __init__(self, _session: Any) -> None:
            pass

        async def consume_order_line(
            self, *, master_sku_id: int, quantity: int, source: Any, occurred_at: Any = None
        ) -> Any:
            calls.append((master_sku_id, quantity))

            class _Applied:
                unmanaged = False
                events = 1

            return _Applied()

    monkeypatch.setattr("app.services.mapping.InventoryService", _Recorder)
    return calls


async def test_the_historical_pass_fills_the_sku_and_moves_no_stock(
    _no_real_inventory: list[tuple[int, int]],
) -> None:
    """The W7-5 rule. The shelf was counted; the sales history was not."""
    item = _Item("r-sku00000041", quantity=3)
    session = _Session([(item, _Order(1))], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert item.master_sku_id == 88
    assert outcome.lines_filled == 1
    assert outcome.stock_events == 0
    assert _no_real_inventory == []


async def test_the_live_pass_applies_the_line_to_stock(
    _no_real_inventory: list[tuple[int, int]],
) -> None:
    item = _Item("SHOP-1", quantity=3)
    session = _Session([(item, _Order(1))], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=True)  # type: ignore[arg-type]

    assert outcome.stock_events == 1
    assert _no_real_inventory == [(88, 3)]


async def test_a_cancelled_order_is_mapped_but_never_consumed(
    _no_real_inventory: list[tuple[int, int]],
) -> None:
    """It was never consumed, so there is nothing to consume now — and a later
    compensation would credit stock the order never took."""
    item = _Item("SHOP-1")
    session = _Session([(item, _Order(1, status="cancelled"))], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=True)  # type: ignore[arg-type]

    assert item.master_sku_id == 88
    assert outcome.cancelled_skipped == 1
    assert _no_real_inventory == []


async def test_a_line_with_no_mapping_is_reported_not_skipped_silently() -> None:
    """A SKU nobody has mapped is the operator's next task, so it has to carry
    enough to rank it: how many lines, how many units, over what period."""
    session = _Session(
        [
            (_Item("MISSING", quantity=2), _Order(1)),
            (_Item("MISSING", quantity=5, line_id="L-2"), _Order(2)),
        ],
        lookups=[None, None],
        still_unmapped=[],
    )

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.lines_filled == 0
    stat = outcome.unresolved[("rakuten", "MISSING")]
    assert (stat.lines, stat.units) == (2, 7)
    assert stat.first_ordered_at == datetime(2026, 3, 4, tzinfo=UTC)


async def test_an_order_still_missing_a_sku_is_not_confirmed() -> None:
    order = _Order(1)
    session = _Session([(_Item("SKU-A"), order)], lookups=[88], still_unmapped=[1])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.orders_settled == 0
    assert order.status == OrderStatusEnum.PENDING_MAPPING


async def test_an_order_that_is_fully_mapped_is_confirmed() -> None:
    order = _Order(1)
    session = _Session([(_Item("SKU-A"), order)], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.orders_settled == 1
    assert order.status == OrderStatusEnum.CONFIRMED


async def test_an_already_confirmed_order_is_left_alone() -> None:
    """A line stranded on a 確定 order is why this scans lines at all. Filling
    it in is the fix; re-confirming an order that was never pending is noise."""
    order = _Order(1, status=OrderStatusEnum.CONFIRMED)
    session = _Session([(_Item("SKU-A"), order)], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.lines_filled == 1
    assert outcome.orders_settled == 0
    assert order.status == OrderStatusEnum.CONFIRMED


async def test_nothing_unmapped_means_no_writes_at_all() -> None:
    session = _Session([], lookups=[], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=True)  # type: ignore[arg-type]

    assert outcome.lines_filled == 0
    assert outcome.unresolved == {}
    assert session.flushes == 0


def test_the_historical_cli_never_applies_stock() -> None:
    """Read as source, because the value is a literal in one place and the
    whole safety of the historical pass rests on it staying False."""
    from pathlib import Path

    source = Path("app/cli/reresolve_order_items.py").read_text(encoding="utf-8")
    assert "apply_stock=False" in source
    assert "apply_stock=True" not in source


# --- 金額 ------------------------------------------------------------------


async def test_the_filled_revenue_is_reported_not_just_the_line_count(
    _no_real_inventory: list[tuple[int, int]],
) -> None:
    """検収 asks what share of revenue is attributable to nothing. 734明細
    could be ¥40,000 or ¥400,000, and only the yen figure answers it."""
    item = _Item("r-sku00000041", quantity=3)
    item.unit_price = Decimal("1200")
    session = _Session([(item, _Order(1))], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.filled_sales_jpy == Decimal("3600")


async def test_the_remaining_unmapped_revenue_is_reported_too() -> None:
    """The half that mapping work still has to reach."""
    item = _Item("STILL-UNKNOWN", quantity=2)
    item.unit_price = Decimal("2500")
    session = _Session([(item, _Order(1))], lookups=[None], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.filled_sales_jpy == Decimal("0")
    assert outcome.unresolved_sales_jpy == Decimal("5000")


async def test_a_cancelled_line_contributes_no_revenue_to_either_figure(
    _no_real_inventory: list[tuple[int, int]],
) -> None:
    """Matching how the KPI counts 未マッピング売上 — otherwise this figure
    would not be comparable with the one on the screen."""
    item = _Item("r-sku00000041", quantity=3)
    item.unit_price = Decimal("1200")
    session = _Session([(item, _Order(1, status="cancelled"))], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.lines_filled == 1
    assert outcome.filled_sales_jpy == Decimal("0")


# --- 再集計の範囲 ----------------------------------------------------------


async def test_the_filled_span_is_recorded_for_the_rebuild(
    _no_real_inventory: list[tuple[int, int]],
) -> None:
    """The rebuild picks its days from inventory events and order updates, and
    this pass produces neither. Without the span it rebuilds nothing, reports
    success, and the dashboards keep the old attribution — which is exactly
    what happened on the first production run."""
    old = _Order(1)
    old.ordered_at = datetime(2025, 10, 3, tzinfo=UTC)
    recent = _Order(2)
    recent.ordered_at = datetime(2026, 9, 21, tzinfo=UTC)
    session = _Session(
        [(_Item("A"), old), (_Item("B"), recent)],
        lookups=[88, 99],
        still_unmapped=[],
    )

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.filled_first_day == date(2025, 10, 3)
    assert outcome.filled_last_day == date(2026, 9, 21)


async def test_an_unresolved_line_does_not_widen_the_rebuild_span() -> None:
    """Only what changed needs rebuilding. A line nobody could map changed
    nothing, and rebuilding its day is wasted work on 400 days of history."""
    unresolvable = _Order(1)
    unresolvable.ordered_at = datetime(2024, 1, 1, tzinfo=UTC)
    session = _Session([(_Item("NOPE"), unresolvable)], lookups=[None], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.filled_first_day is None


async def test_the_span_is_taken_in_jst() -> None:
    """15:30 UTC is 00:30 JST the next day. A UTC span can start a day early
    and, worse, end a day short of the row that actually changed."""
    order = _Order(1)
    order.ordered_at = datetime(2026, 9, 21, 15, 30, tzinfo=UTC)
    session = _Session([(_Item("A"), order)], lookups=[88], still_unmapped=[])

    outcome = await reresolve_unmapped_lines(session, apply_stock=False)  # type: ignore[arg-type]

    assert outcome.filled_last_day == date(2026, 9, 22)


def test_the_cli_hands_the_rebuild_an_explicit_range() -> None:
    """Read as source. `max_days=0` means "no cap", NOT "every day", and the
    difference is silent: detection returns an empty list and the run reports
    success having rebuilt nothing."""
    from pathlib import Path

    source = Path("app/cli/reresolve_order_items.py").read_text(encoding="utf-8")
    assert "from_date=first" in source
    assert "to_date=last" in source
