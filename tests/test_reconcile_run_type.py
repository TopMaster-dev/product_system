"""One table, three kinds of run — and only a column telling them apart.

Since migration 0012, `reconcile_runs` holds the daily CROSS MALL
reconciliation, the Shopify stock audit that replaces it when CROSS MALL shuts
down, and the physical stocktake. They share the approval path deliberately: it
is the only code that overwrites a snapshot outright, and maintaining three
copies of that is worse than one discriminator.

The cost of sharing is that EVERY query meaning one kind must say so. Miss one
and an audit run appears in the operator's queue as work they are asked to
approve, or the dashboard badge counts it — a screen quietly lying about what
needs doing. docs/23 records that the dashboard count was missed by every one of
the five area designs, which is why the last test here reads the source.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from app.models.enums import ReconcileRunTypeEnum
from app.services.reconcile import of_run_type

pytestmark = pytest.mark.unit

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"


def _sql(condition) -> str:
    return str(condition.compile(compile_kwargs={"literal_binds": True}))


def test_the_default_is_the_crossmall_reconciliation() -> None:
    """Existing call sites meant CROSS MALL before 0012 and must still mean it."""
    assert "run_type = 'reconcile'" in _sql(of_run_type())


def test_each_kind_can_be_named() -> None:
    for kind in ReconcileRunTypeEnum:
        assert f"run_type = '{kind.value}'" in _sql(of_run_type(kind))


def test_a_plain_string_works_too() -> None:
    """Call sites in migrations and CLIs pass the literal."""
    assert _sql(of_run_type("stocktake")) == _sql(of_run_type(ReconcileRunTypeEnum.STOCKTAKE))


# Modules allowed to query ReconcileRun without naming a run_type, with reasons.
UNFILTERED_ALLOWED = {
    # Defines the predicate. `_get_run` is type-agnostic on purpose — the
    # service applies whatever the caller already authorised, and each route
    # guards its own kind before calling in.
    "app/services/reconcile.py",
    # Writes runs; the run_type is set at creation, not filtered.
    "app/cli/reconcile_inventory.py",
}

# `select(ReconcileRun)` / `select_from(ReconcileRun)` — reading the table.
READS_RUNS = re.compile(r"select(?:_from)?\(\s*ReconcileRun\b")


def test_every_module_that_reads_runs_names_a_run_type() -> None:
    """The guard docs/23 says every area design needed and none had."""
    offenders = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR.parent).as_posix()
        if rel in UNFILTERED_ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        if READS_RUNS.search(source) and "of_run_type" not in source:
            offenders.append(rel)
    assert not offenders, (
        "these read reconcile_runs without naming a run_type — a Shopify audit or "
        f"stocktake will surface as a pending CROSS MALL reconciliation: {offenders}"
    )


def test_the_reconcile_screen_guards_reads_and_writes_alike() -> None:
    """The detail GET refuses another kind; the approve/skip POSTs are reachable
    by id alone and must refuse it too. A screen that will not display a run
    must not mutate it."""
    source = (APP_DIR / "ui" / "routes" / "reconcile.py").read_text(encoding="utf-8")
    guards = source.count("run.run_type != ReconcileRunTypeEnum.RECONCILE.value")
    assert guards >= 2, "expected the guard on both the detail view and the diff actions"
