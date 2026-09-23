"""`session.begin()` の後に読み取りを置かない.

SQLAlchemy autobegins a transaction on the first read. So a handler that reads
through the session and THEN opens `async with session.begin()` asks for a
second transaction and dies with:

    InvalidRequestError: A transaction is already begun on this Session.

It is invisible in tests whose fake session answers `execute()` without
modelling that a read starts a transaction, and invisible in any test that only
exercises the read-only path. It shows up on the first real write, in
production.

This has now happened twice. First in three CLIs (`sync_shopify_masters`,
`link_shared_stock`, `audit_shopify_stock`), where the fix came with
`test_cli_transactions.py` — a fake session that models autobegin, covering
`app/cli`. Then on 2026-09-23 the production smoke test of the stocktake screen
returned 500 from `/admin/stocktake/execute`, and the scan below found the same
shape in FIVE routes: upload, approve, skip, finalize and cancel. The entire
screen was unusable and nothing said so, because the earlier guard only looked
at `app/cli`.

So this one reads the source. It finds every `async with session.begin()` that
has a statement before it in the same function passing `session` to something,
and fails. It cannot tell a read from a write, which is the point: if the work
is already on the session, the transaction is already open.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SCANNED = ("app/cli", "app/ui/routes", "app/api", "app/services")

#: Session methods that do NOT open a transaction, so seeing one before
#: `begin()` is harmless. `add` only stages an object in the identity map.
_SAFE_SESSION_METHODS = {"begin", "commit", "rollback", "close", "add", "add_all", "expunge"}


def _opens_transaction(node: ast.AsyncWith) -> bool:
    """Does this `async with` begin a transaction on an ALREADY-LIVE session?

    `async with factory() as session, session.begin():` does not: the session is
    created right there, so nothing can have used it yet. Only the bare
    `async with session.begin():` inherits whatever state the session is in,
    and that is the shape that breaks.
    """
    creates_session = any(
        isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == "session"
        for item in node.items
    )
    if creates_session:
        return False
    return any(
        isinstance(item.context_expr, ast.Call)
        and isinstance(item.context_expr.func, ast.Attribute)
        and item.context_expr.func.attr == "begin"
        and isinstance(item.context_expr.func.value, ast.Name)
        and item.context_expr.func.value.id == "session"
        for item in node.items
    )


def _touches_session(node: ast.AST) -> bool:
    """Does this statement hand `session` to anything, or call a method on it?

    Both forms matter. `await session.execute(...)` is the obvious one;
    `await build_plan(session, data)` is the one that actually shipped, and a
    check looking only for `session.` would have missed it.
    """
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
            and func.attr not in _SAFE_SESSION_METHODS
        ):
            return True
        for arg in [*n.args, *(k.value for k in n.keywords)]:
            if isinstance(arg, ast.Name) and arg.id == "session":
                return True
    return False


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for index, stmt in enumerate(fn.body):
            if isinstance(stmt, ast.AsyncWith) and _opens_transaction(stmt):
                if any(_touches_session(earlier) for earlier in fn.body[:index]):
                    found.append(f"{path.as_posix()}:{stmt.lineno} {fn.name}()")
                break
    return found


def _sources() -> list[Path]:
    return sorted(p for folder in SCANNED for p in Path(folder).rglob("*.py"))


def test_the_scan_is_reading_real_code() -> None:
    """Without this, an empty file list makes every assertion below vacuous."""
    files = _sources()
    assert len(files) > 40, f"only found {len(files)} modules; the scan is broken"
    assert any("session.begin()" in p.read_text(encoding="utf-8") for p in files), (
        "no module opens a transaction at all — the pattern this guards has moved"
    )


def test_nothing_reads_through_the_session_before_opening_a_transaction() -> None:
    offenders = [o for path in _sources() for o in _offenders(path)]
    assert not offenders, (
        "these open a transaction after the session has already been used, which "
        "raises InvalidRequestError on the first real request:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_detects_the_shape_it_is_looking_for() -> None:
    """The guard's own guard. If `_touches_session` stopped recognising a
    service call taking `session`, the suite would go quiet and report nothing
    — exactly how the five stocktake routes shipped."""
    source = (
        "async def handler(session):\n"
        "    plan = await build_plan(session, data)\n"
        "    async with session.begin():\n"
        "        session.add(plan)\n"
    )
    tree = ast.parse(source)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _touches_session(fn.body[0])
    assert isinstance(fn.body[1], ast.AsyncWith)
    assert _opens_transaction(fn.body[1])


def test_a_transaction_opened_first_is_not_flagged() -> None:
    """The correct shape must stay usable: open, then work."""
    source = (
        "async def handler(session):\n"
        "    async with session.begin():\n"
        "        await service.do(session)\n"
    )
    fn = ast.parse(source).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    stmt = fn.body[0]
    assert isinstance(stmt, ast.AsyncWith)
    assert _opens_transaction(stmt)
    assert not any(_touches_session(s) for s in fn.body[:0])


def test_a_session_created_in_the_same_with_is_not_flagged() -> None:
    """`async with factory() as session, session.begin():` is the correct CLI
    shape — the session is born there, so it cannot already be in a
    transaction. Flagging it made the guard fail on `adjust_inventory`, which
    is not broken; a guard with a false positive gets switched off."""
    source = (
        "async def run(factory):\n"
        "    if dry_run:\n"
        "        async with factory() as session:\n"
        "            before = await svc.read(session)\n"
        "    async with factory() as session, session.begin():\n"
        "        await svc.write(session)\n"
    )
    fn = ast.parse(source).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    inner = fn.body[-1]
    assert isinstance(inner, ast.AsyncWith)
    assert not _opens_transaction(inner)
