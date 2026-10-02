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


def _is_session_touch(call: ast.Call) -> bool:
    func = call.func
    if (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "session"
        and func.attr not in _SAFE_SESSION_METHODS
    ):
        return True
    return any(
        isinstance(arg, ast.Name) and arg.id == "session"
        for arg in [*call.args, *(k.value for k in call.keywords)]
    )


def _ends_transaction(call: ast.Call) -> bool:
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "session"
        and func.attr in {"commit", "rollback"}
    )


def _touches_session(node: ast.AST) -> bool:
    """Does this statement hand `session` to anything, or call a method on it?

    Both forms matter. `await session.execute(...)` is the obvious one;
    `await build_plan(session, data)` is the one that actually shipped, and a
    check looking only for `session.` would have missed it.
    """
    return any(isinstance(n, ast.Call) and _is_session_touch(n) for n in ast.walk(node))


def _own_nodes(fn: ast.AST) -> list[ast.AST]:
    """Every node of `fn`, at any depth, without descending into nested defs —
    those have their own session and are scanned on their own."""
    found: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef | ast.Lambda):
            continue
        found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _offenders_in(fn: ast.AsyncFunctionDef | ast.FunctionDef, path: str) -> list[str]:
    """Walk the function in source order, at ANY nesting depth.

    The first version only looked at top-level statements, so it never saw
    `try: async with session.begin():` — and `_diff_action` in the reconcile
    screen read the run with `session.get()` and then did exactly that. Every
    approve and skip on that screen raised, and the guard was green.

    Source order stands in for execution order: a touch, then a `begin()` with
    no commit or rollback in between, is the shape that raises. Leaving a
    `begin()` block also ends its transaction.
    """
    events: list[tuple[int, int, ast.AST]] = []  # (line, order, node)
    for node in _own_nodes(fn):
        if isinstance(node, ast.AsyncWith) and _opens_transaction(node):
            events.append((node.lineno, 1, node))
            events.append((node.end_lineno or node.lineno, 2, node))
        elif isinstance(node, ast.Call) and (_is_session_touch(node) or _ends_transaction(node)):
            events.append((node.lineno, 0, node))

    found: list[str] = []
    touched = False
    for line, order, node in sorted(events, key=lambda e: (e[0], e[1])):
        if order == 1:
            if touched:
                found.append(f"{path}:{line} {fn.name}()")
            touched = False
        elif order == 2:
            touched = False
        elif isinstance(node, ast.Call):
            touched = not _ends_transaction(node)
    return found


def _offenders(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        offender
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
        for offender in _offenders_in(fn, path.as_posix())
    ]


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


def test_a_transaction_opened_inside_a_try_is_still_seen() -> None:
    """The shape the first version missed: `_diff_action` in the reconcile
    screen, where every approve and skip raised in production."""
    source = (
        "async def handler(session):\n"
        "    run = await session.get(Run, 1)\n"
        "    if run is None:\n"
        "        return None\n"
        "    try:\n"
        "        async with session.begin():\n"
        "            await svc.approve(session)\n"
        "    except ValueError:\n"
        "        return None\n"
    )
    fn = ast.parse(source).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _offenders_in(fn, "x.py") == ["x.py:6 handler()"]


def test_a_commit_between_the_read_and_the_transaction_clears_it() -> None:
    source = (
        "async def handler(session):\n"
        "    await session.execute(stmt)\n"
        "    await session.commit()\n"
        "    async with session.begin():\n"
        "        await svc.write(session)\n"
        "    async with session.begin():\n"
        "        await svc.write(session)\n"
    )
    fn = ast.parse(source).body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    assert _offenders_in(fn, "x.py") == []
