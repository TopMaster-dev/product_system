"""検収突合シートの読み方 (P2-042).

The sheet exists to be handed to a client who is checking our arithmetic. Two
things therefore have to be true of it before the numbers are even looked at:
a disagreement must be impossible to miss, and an agreement must not be
claimed for a day that was never compared.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.cli.verify_analytics_totals import (
    CSV_HEADER,
    DayRow,
    print_report,
    whole_days_before_today,
    write_csv,
)
from app.services.analytics_audit import Measure, RawTotals, Reconciliation
from app.services.analytics_query import Kpis
from app.services.timeframe import Period

pytestmark = pytest.mark.unit


def _day(**over: object) -> DayRow:
    base: dict[str, object] = {
        "day": date(2026, 9, 15),
        "screen_sales": Decimal("128400"),
        "orders_sales": Decimal("128400"),
        "screen_quantity": 46,
        "orders_quantity": 46,
        "screen_order_count": 31,
        "orders_order_count": 31,
        "screen_unmapped": Decimal("0"),
        "orders_unmapped": Decimal("0"),
        "cancelled_sales": Decimal("0"),
    }
    base.update(over)
    return DayRow(**base)  # type: ignore[arg-type]


# --- 期間の取り方 ----------------------------------------------------------


def test_the_window_ends_yesterday_not_today() -> None:
    """Today is still accumulating and its rollup ran at best an hour ago.
    Including it manufactures a disagreement on every single run."""
    period = whole_days_before_today(7, now=datetime(2026, 9, 23, 3, 0, tzinfo=UTC))
    assert period.last_day == date(2026, 9, 22)
    assert period.first_day == date(2026, 9, 16)
    assert period.days == 7


def test_the_window_is_taken_in_jst_not_utc() -> None:
    """00:30 JST on the 23rd is 15:30 UTC on the 22nd. Reading the UTC date
    would shift the whole window by a day."""
    period = whole_days_before_today(7, now=datetime(2026, 9, 22, 15, 30, tzinfo=UTC))
    assert period.last_day == date(2026, 9, 22)


# --- 一致/不一致の判定 -----------------------------------------------------


def test_a_matching_day_agrees() -> None:
    assert _day().agrees is True


def test_a_sales_difference_of_one_yen_is_a_disagreement() -> None:
    """No tolerance, on purpose. Both sides are integer yen off the same
    orders, so a rounding allowance would only hide a real gap."""
    assert _day(screen_sales=Decimal("128401")).agrees is False


def test_quantity_is_compared_even_when_the_money_matches() -> None:
    """A wrong unit price on one line and a missing line on another can cancel
    out in yen. The quantity does not."""
    assert _day(screen_quantity=45).agrees is False


def test_order_count_is_compared_too() -> None:
    assert _day(orders_order_count=30).agrees is False


def test_the_difference_keeps_its_sign() -> None:
    """Which side is high is the first thing that narrows the cause."""
    assert _day(screen_sales=Decimal("130000")).sales_difference == Decimal("1600")
    assert _day(orders_sales=Decimal("130000")).sales_difference == Decimal("-1600")


# --- Measure ---------------------------------------------------------------


def test_a_measure_with_no_difference_agrees() -> None:
    assert Measure("売上金額", Decimal("100"), Decimal("100")).agrees is True


def test_a_measure_reports_the_gap() -> None:
    m = Measure("売上金額", Decimal("100"), Decimal("140"))
    assert m.agrees is False
    assert m.difference == Decimal("-40")


# --- レポート --------------------------------------------------------------


def _kpis(**over: object) -> Kpis:
    base: dict[str, object] = {
        "gross_sales_jpy": Decimal("128400"),
        "sold_quantity": 46,
        "order_count": 31,
        "unmapped_sales_jpy": Decimal("0"),
        "total_on_hand_qty": 5000,
        "out_of_stock_sku_count": 12,
        "sku_count": 650,
        "average_on_hand_qty": 5010.0,
        "days_with_data": 7,
    }
    base.update(over)
    return Kpis(**base)  # type: ignore[arg-type]


def _raw(**over: object) -> RawTotals:
    base: dict[str, object] = {
        "gross_sales_jpy": Decimal("128400"),
        "sold_quantity": 46,
        "order_count": 31,
        "unmapped_sales_jpy": Decimal("0"),
        "cancelled_sales_jpy": Decimal("0"),
        "cancelled_quantity": 0,
        "unmapped_line_count": 0,
    }
    base.update(over)
    return RawTotals(**base)  # type: ignore[arg-type]


def _result(*, measures: list[Measure] | None = None, missing: list[date] | None = None):
    period = Period(date(2026, 9, 16), date(2026, 9, 22), "custom")
    return Reconciliation(
        period=period,
        kpis=_kpis(),
        raw=_raw(),
        measures=measures
        or [Measure("売上金額 (マッピング済)", Decimal("128400"), Decimal("128400"))],
        missing_days=missing or [],
    )


def test_an_agreement_is_stated_plainly(capsys: pytest.CaptureFixture[str]) -> None:
    print_report(_result(), [_day()])
    assert "すべて一致しました" in capsys.readouterr().out


def test_a_disagreement_is_marked_on_the_line_and_counted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = [Measure("売上金額 (マッピング済)", Decimal("130000"), Decimal("128400"))]
    print_report(_result(measures=broken), [_day()])
    out = capsys.readouterr().out
    assert "★" in out
    assert "1項目が一致しません" in out
    assert "すべて一致しました" not in out


def test_the_days_with_no_rollup_row_are_named_not_counted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """「28日中26日」does not say WHICH two, and the two dates are what makes
    the gap explainable."""
    print_report(_result(missing=[date(2026, 9, 18), date(2026, 9, 19)]), [_day()])
    out = capsys.readouterr().out
    assert "2026-09-18" in out
    assert "2026-09-19" in out
    assert "rebuild_daily_metrics" in out


def test_the_variance_factors_are_always_shown(capsys: pytest.CaptureFixture[str]) -> None:
    """Cancellations and unmapped lines explain most real gaps. Printing them
    only when something disagrees means the reader meets them for the first
    time at the worst moment."""
    print_report(_result(), [_day()])
    out = capsys.readouterr().out
    assert "キャンセル済の売上" in out
    assert "未マッピングの明細" in out


def test_the_daily_table_marks_the_offending_date(capsys: pytest.CaptureFixture[str]) -> None:
    """The reason the daily table exists at all."""
    rows = [_day(), _day(day=date(2026, 9, 16), screen_sales=Decimal("99999"))]
    print_report(_result(), rows)
    out = capsys.readouterr().out
    # Scoped to the daily table: the period heading carries dates too.
    table = out.split("--- 日別 ---", 1)[1].splitlines()

    bad = next(ln for ln in table if "2026-09-16" in ln)
    good = next(ln for ln in table if "2026-09-15" in ln)
    assert "★" in bad
    assert "★" not in good


# --- CSV -------------------------------------------------------------------


def test_the_csv_row_matches_the_header_width() -> None:
    """A column that silently slides one position turns the sheet handed to the
    client into a wrong answer that still looks tidy."""
    assert len(_day().as_csv()) == len(CSV_HEADER)


def test_the_csv_is_written_with_a_bom_for_excel(tmp_path) -> None:
    """The client opens it in Excel. Without the BOM the Japanese headers are
    mojibake and the sheet is unusable."""
    period = Period(date(2026, 9, 16), date(2026, 9, 22), "custom")
    path = write_csv(tmp_path, period, [_day()])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert "2026-09-16" in path.name
    assert "2026-09-22" in path.name


def test_the_csv_carries_both_sides_and_the_difference(tmp_path) -> None:
    period = Period(date(2026, 9, 16), date(2026, 9, 22), "custom")
    path = write_csv(tmp_path, period, [_day(screen_sales=Decimal("130000"))])
    text = path.read_text(encoding="utf-8-sig")
    assert "画面_売上金額" in text
    assert "受注データ_売上金額" in text
    assert "130000" in text
    assert "128400" in text
    assert "1600" in text
