"""定期ジョブが動いたことの判定 (P2-045).

Cloud Scheduler records a run by its HTTP status, so "the console says success"
is not evidence — this service has had three outages that looked healthy from
there. The judgement therefore has to come from what the work left behind, and
the part that is easy to get wrong is the arithmetic around a day that is not
over yet.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.cli.inspect_scheduler_health import DayHealth, report

pytestmark = pytest.mark.unit

TODAY = date(2026, 9, 23)
YESTERDAY = date(2026, 9, 22)
#: 11:51 JST on the 23rd.
NOW = datetime(2026, 9, 23, 2, 51, tzinfo=UTC)


def _day(day: date = YESTERDAY, **kw: object) -> DayHealth:
    base: dict[str, object] = {"runs": 25, "successes": 25, "failures": 0}
    base.update(kw)
    return DayHealth(day=day, **base)  # type: ignore[arg-type]


# --- 未完了の日 ------------------------------------------------------------


def test_a_complete_day_with_a_full_set_of_runs_is_normal() -> None:
    assert _day().verdict == "正常"


def test_todays_partial_count_is_not_reported_as_a_shortfall() -> None:
    """The first production run flagged 15 runs at 11:51 JST as low, because it
    compared a third of a day against a whole one. A check that cries wolf
    every morning is a check nobody reads."""
    today = _day(day=TODAY, runs=15, successes=15, hours_elapsed=12)
    assert today.verdict == "進行中"
    assert today.partial is True


def test_a_partial_day_that_is_genuinely_short_is_still_flagged() -> None:
    """Scaling the expectation must not switch the check off."""
    today = _day(day=TODAY, runs=2, successes=2, hours_elapsed=12)
    assert "少なめ" in today.verdict


def test_a_complete_day_that_is_short_is_flagged() -> None:
    assert "少なめ" in _day(runs=8, successes=8).verdict


# --- 失敗と不在 ------------------------------------------------------------


def test_a_day_with_no_successful_run_is_the_headline() -> None:
    """Absence is the finding. A job that never fired logs no failures either,
    and "no failures" is the reading that hides an outage."""
    day = _day(runs=0, successes=0)
    assert day.verdict == "★ 成功した実行がありません"
    assert day.healthy is False


def test_a_failure_outranks_a_low_count() -> None:
    day = _day(runs=3, successes=2, failures=1)
    assert day.verdict == "★ 失敗 1件"


# --- 2日連続 ---------------------------------------------------------------


def _report(days: list[DayHealth], **kw: object) -> tuple[int, str]:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    defaults: dict[str, object] = {
        "latest_velocity": datetime(2026, 9, 23, 2, 20, tzinfo=UTC),
        "velocity_rows": 678,
        "now": NOW,
    }
    defaults.update(kw)
    with redirect_stdout(buf):
        problems = report(days, **defaults)  # type: ignore[arg-type]
    return problems, buf.getvalue()


def test_two_healthy_days_satisfy_the_requirement() -> None:
    problems, out = _report([_day(day=TODAY, hours_elapsed=12), _day()])
    assert problems == 0
    assert "充足" in out


def test_a_failure_yesterday_breaks_the_streak() -> None:
    problems, out = _report([_day(day=TODAY, hours_elapsed=12), _day(failures=2)])
    assert problems > 0
    assert "未充足" in out


def test_one_day_of_history_cannot_satisfy_two_consecutive_days() -> None:
    problems, out = _report([_day(day=TODAY, hours_elapsed=12)])
    assert problems > 0
    assert "未充足" in out


# --- 販売速度の鮮度 --------------------------------------------------------


def test_a_stale_velocity_table_is_flagged() -> None:
    """The table is rewritten by every rollup pass that did work, so a stale
    timestamp means the passes ran and achieved nothing — which is precisely
    the shape that looks healthy from the Scheduler console."""
    problems, out = _report(
        [_day(day=TODAY, hours_elapsed=12), _day()],
        latest_velocity=datetime(2026, 9, 22, 2, 20, tzinfo=UTC),
    )
    assert problems > 0
    assert "★" in out


def test_a_velocity_table_that_was_never_computed_is_flagged() -> None:
    problems, out = _report(
        [_day(day=TODAY, hours_elapsed=12), _day()],
        latest_velocity=None,
        velocity_rows=0,
    )
    assert problems > 0
    assert "一度も計算されていません" in out


def test_a_fresh_velocity_table_passes() -> None:
    problems, out = _report([_day(day=TODAY, hours_elapsed=12), _day()])
    assert problems == 0
    assert "問題は見つかりませんでした" in out
