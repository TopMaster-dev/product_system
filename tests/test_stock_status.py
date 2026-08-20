"""Unit tests for the stock-status bucket definition.

`classify()` (Python, used by CSV export) and `filter_condition()` /
`status_rank()` (SQL, used by the list and the badges) are two expressions of one
rule. If they drift, the CSV says 低在庫 for a row the screen calls 正常. These
tests pin both to the same boundaries.
"""

from __future__ import annotations

import pytest

from app.services.stock_status import (
    DEFAULT_LOW_STOCK_THRESHOLD,
    STATUS_LABELS,
    STATUS_ORDER,
    StockStatus,
    classify,
    count_expression,
    filter_condition,
    status_rank,
)

pytestmark = pytest.mark.unit


def _sql(expression) -> str:
    return str(expression.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.parametrize(
    ("qty", "expected"),
    [
        (-100, StockStatus.NEGATIVE),
        (-1, StockStatus.NEGATIVE),
        (0, StockStatus.ZERO),
        (1, StockStatus.LOW),
        (9, StockStatus.LOW),
        (10, StockStatus.NORMAL),
        (999, StockStatus.NORMAL),
    ],
)
def test_classify_boundaries(qty: int, expected: StockStatus) -> None:
    assert classify(qty) is expected


def test_threshold_is_a_parameter_not_a_constant() -> None:
    """W6 replaces the flat 10 with a per-SKU value; nothing here may hard-code it."""
    assert classify(15, threshold=20) is StockStatus.LOW
    assert classify(15, threshold=10) is StockStatus.NORMAL
    assert "20" in _sql(status_rank(20))


def test_every_status_has_a_label_and_a_rank() -> None:
    assert set(STATUS_ORDER) == set(StockStatus)
    assert set(STATUS_LABELS) == set(StockStatus)
    # Display order is triage order: problems first.
    assert STATUS_ORDER[0] is StockStatus.NEGATIVE
    assert STATUS_ORDER[-1] is StockStatus.NORMAL


def test_buckets_are_mutually_exclusive_and_total() -> None:
    """Every quantity lands in exactly one bucket — the property the badges rely
    on for `negative + zero + low + normal == total`."""
    for qty in range(-5, 30):
        matched = [s for s in StockStatus if _matches(s, qty, DEFAULT_LOW_STOCK_THRESHOLD)]
        assert matched == [classify(qty)], f"qty={qty} matched {matched}"


def _matches(status: StockStatus, qty: int, threshold: int) -> bool:
    """Python mirror of `filter_condition`, used only to prove exclusivity."""
    if status is StockStatus.NEGATIVE:
        return qty < 0
    if status is StockStatus.ZERO:
        return qty == 0
    if status is StockStatus.LOW:
        return 1 <= qty < threshold
    return qty >= threshold


def test_count_expression_is_built_from_filter_condition() -> None:
    """The badge and the filtered view must not be able to disagree: the count
    SQL has to contain the very predicate the filter uses."""
    for status in StockStatus:
        predicate = _sql(filter_condition(status, 10))
        assert predicate in _sql(count_expression(status, 10))


def test_expressions_are_built_fresh_each_call() -> None:
    """Builders, not module-level constants — a shared expression object cannot
    carry a per-request threshold, and an unbound bindparam inside one raises
    StatementError once the query is wrapped in select(count())."""
    assert status_rank(10) is not status_rank(10)
    assert _sql(filter_condition(StockStatus.LOW, 5)) != _sql(filter_condition(StockStatus.LOW, 50))
