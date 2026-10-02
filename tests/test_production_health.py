"""本番の健全性チェックを1コマンドで (検収当日の朝).

What matters about a one-line verdict is that it cannot be more optimistic than
the reports it summarises, and that it cannot change anything. These tests pin
the parts that do not need a database; the queries themselves are exercised in
tests/integration/test_production_health.py.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.cli.verify_production_health import (
    Check,
    Outcome,
    print_report,
    run_check,
    script_head,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 7, 0, 30, tzinfo=UTC)  # 09:30 JST


class _RecordingSession:
    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, stmt: Any) -> None:
        self.statements.append(str(stmt))

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _factory(session: _RecordingSession) -> Any:
    return lambda: session


async def test_every_check_starts_a_read_only_transaction() -> None:
    """The guarantee that this cannot write rests on one statement, issued
    before the check sees the session. Postgres then refuses any write in that
    transaction, whatever a reused query happens to do."""
    session = _RecordingSession()
    seen: list[list[str]] = []

    async def check(s: Any) -> Outcome:
        seen.append(list(s.statements))
        return Outcome(True, "ok")

    await run_check(_factory(session), "x", "detail", check)
    assert seen == [["SET TRANSACTION READ ONLY"]]


async def test_a_crashing_check_is_reported_not_raised() -> None:
    """One broken check must not hide the verdict of the other five."""

    async def check(_: Any) -> Outcome:
        raise RuntimeError("relation does not exist")

    result = await run_check(_factory(_RecordingSession()), "索引", "the cli", check)
    assert result.ok is False
    assert "relation does not exist" in result.summary
    assert result.detail == "the cli"


async def test_a_passing_check_keeps_its_notes() -> None:
    async def check(_: Any) -> Outcome:
        return Outcome(True, "一致", ("未マッピング売上 1円",))

    result = await run_check(_factory(_RecordingSession()), "突合", "cli", check)
    assert result == Check("突合", True, "一致", "cli", ("未マッピング売上 1円",))


def _printed(checks: list[Check]) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        print_report(checks, now=NOW)
    return buf.getvalue()


def test_all_passing_says_so_and_names_no_commands() -> None:
    out = _printed([Check("索引", True, "4クエリすべて索引あり", "cli-a")])
    assert "[OK] 索引" in out
    assert "すべて合格です。" in out
    assert "cli-a" not in out
    assert "2026-10-07 09:30 JST" in out


def test_a_failure_names_the_command_that_explains_it() -> None:
    out = _printed(
        [
            Check("索引", True, "ok", "cli-a"),
            Check("在庫スナップショット", False, "乖離 3件", "cli-b"),
        ]
    )
    assert "[NG] 在庫スナップショット" in out
    assert "不合格 1件" in out
    assert "cli-b" in out
    assert "cli-a" not in out


def test_notes_are_printed_even_when_everything_passes() -> None:
    """The unmapped share is not a pass/fail measure, but it is the number the
    client will ask about first."""
    out = _printed([Check("突合", True, "一致", "cli", ("未マッピング売上 311,010円",))])
    assert "参考: 未マッピング売上 311,010円" in out


def test_the_migration_head_resolves_from_any_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """alembic.ini names `script_location` relative to the repo root. Run from
    anywhere else, a relative lookup would raise, and the check would report
    the migration as broken on a healthy system."""
    newest = sorted(Path("alembic/versions").glob("[0-9][0-9][0-9][0-9]_*.py"))[-1]
    monkeypatch.chdir(tmp_path)
    assert script_head() == newest.name[:4]
