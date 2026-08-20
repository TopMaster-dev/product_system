"""Stock-status buckets, defined once.

The four buckets (マイナス / ゼロ / 低在庫 / 正常) were open-coded in five Python
sites and seven template sites, each repeating the literal threshold `10`. That
is why the count badges could disagree with the list they label: the badge query
and the row query expressed "low stock" separately.

Everything here takes the threshold as an ARGUMENT rather than reading a module
constant. W6 replaces the fixed 10 with a per-SKU value derived from sales
velocity, and the only thing that has to change then is what gets passed in.

SQL expressions are produced by BUILDER FUNCTIONS, never module-level constants:
a module-level expression is evaluated at import, so it cannot carry a
per-request threshold, and an unbound bindparam inside one would blow up as
`StatementError: A value is required for bind parameter` the moment the query is
wrapped in a `select(count()).select_from(...)`.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import ColumnElement, Integer, case, func

from app.models import InventorySnapshot

DEFAULT_LOW_STOCK_THRESHOLD = 10


class StockStatus(StrEnum):
    NEGATIVE = "negative"
    ZERO = "zero"
    LOW = "low"
    NORMAL = "normal"


#: Display order = triage order. Problems first, so the default sort surfaces
#: what an operator must act on without them choosing a sort.
STATUS_ORDER: tuple[StockStatus, ...] = (
    StockStatus.NEGATIVE,
    StockStatus.ZERO,
    StockStatus.LOW,
    StockStatus.NORMAL,
)

STATUS_LABELS: dict[StockStatus, str] = {
    StockStatus.NEGATIVE: "マイナス",
    StockStatus.ZERO: "ゼロ",
    StockStatus.LOW: "低在庫",
    StockStatus.NORMAL: "正常",
}


def classify(qty: int, threshold: int = DEFAULT_LOW_STOCK_THRESHOLD) -> StockStatus:
    """Bucket a quantity. The Python counterpart of `status_rank` / `filter_condition`
    — used by CSV export and tests, and kept in lockstep with them by
    `tests/test_stock_status.py`."""
    if qty < 0:
        return StockStatus.NEGATIVE
    if qty == 0:
        return StockStatus.ZERO
    if qty < threshold:
        return StockStatus.LOW
    return StockStatus.NORMAL


def qty_expression() -> ColumnElement[int]:
    """On-hand quantity; a master with no snapshot row counts as 0."""
    return func.coalesce(InventorySnapshot.on_hand_qty, 0)


def status_rank(threshold: int = DEFAULT_LOW_STOCK_THRESHOLD) -> ColumnElement[int]:
    """Sort key matching STATUS_ORDER, so ORDER BY puts problems first."""
    qty = qty_expression()
    return case(
        (qty < 0, 0),
        (qty == 0, 1),
        (qty < threshold, 2),
        else_=3,
    )


def filter_condition(
    status: StockStatus,
    threshold: int = DEFAULT_LOW_STOCK_THRESHOLD,
) -> ColumnElement[bool]:
    """WHERE clause selecting exactly one bucket. Buckets are mutually
    exclusive: `low` is 1..threshold-1, so it excludes zero and negative."""
    qty = qty_expression()
    if status is StockStatus.NEGATIVE:
        return qty < 0
    if status is StockStatus.ZERO:
        return qty == 0
    if status is StockStatus.LOW:
        return (qty >= 1) & (qty < threshold)
    return qty >= threshold


def count_expression(
    status: StockStatus,
    threshold: int = DEFAULT_LOW_STOCK_THRESHOLD,
) -> ColumnElement[int]:
    """SUM(CASE ...) counting one bucket, for the badge row.

    Built from `filter_condition` so a badge can never drift from the filtered
    view it links to — the bug class this module exists to prevent.
    """
    return func.coalesce(
        func.sum(case((filter_condition(status, threshold), 1), else_=0)),
        0,
    ).cast(Integer)
