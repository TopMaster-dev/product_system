"""Unit tests for scripts/reconcile_admin.py.

Argparse + sub-command dispatch + the finalize-notes sentinel check are
covered here. The underlying ReconcileService behavior is fully covered
by tests/test_reconcile_service.py — we don't re-test it from here.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from reconcile_admin import (  # noqa: E402
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    VERIFICATION_NOTES_PREFIX,
    cmd_finalize,
    main,
    parse_args,
)

# ---------- argparse / dispatch ----------


@pytest.mark.unit
def test_start_subcommand_args() -> None:
    args = parse_args(["start", "--csv", "./x.csv", "--triggered-by", "sre-verify"])
    assert args.cmd == "start"
    assert args.csv == "./x.csv"
    assert args.triggered_by == "sre-verify"


@pytest.mark.unit
def test_dry_run_subcommand_args() -> None:
    args = parse_args(["dry-run", "--csv", "gs://bkt/x.csv", "--triggered-by", "sre"])
    assert args.cmd == "dry-run"


@pytest.mark.unit
def test_list_subcommand_requires_run_id() -> None:
    with pytest.raises(SystemExit):
        parse_args(["list"])


@pytest.mark.unit
def test_list_subcommand_args() -> None:
    args = parse_args(["list", "--run-id", "7"])
    assert args.cmd == "list"
    assert args.run_id == 7


@pytest.mark.unit
def test_approve_subcommand_args() -> None:
    args = parse_args(["approve", "--diff-id", "42", "--approved-by", "sre"])
    assert args.cmd == "approve"
    assert args.diff_id == 42
    assert args.approved_by == "sre"


@pytest.mark.unit
def test_skip_subcommand_args() -> None:
    args = parse_args(["skip", "--diff-id", "9", "--approved-by", "op"])
    assert args.cmd == "skip"


@pytest.mark.unit
def test_finalize_subcommand_args() -> None:
    args = parse_args(
        [
            "finalize",
            "--run-id",
            "7",
            "--approved-by",
            "sre",
            "--notes",
            f"{VERIFICATION_NOTES_PREFIX}2026-06-16",
        ]
    )
    assert args.cmd == "finalize"
    assert args.run_id == 7
    assert args.notes.startswith(VERIFICATION_NOTES_PREFIX)


@pytest.mark.unit
def test_cancel_subcommand_args() -> None:
    args = parse_args(
        ["cancel", "--run-id", "5", "--cancelled-by", "sre", "--reason", "invalid csv"]
    )
    assert args.cmd == "cancel"
    assert args.run_id == 5
    assert args.reason == "invalid csv"


@pytest.mark.unit
def test_unknown_subcommand_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["unknown-cmd"])


@pytest.mark.unit
def test_no_subcommand_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args([])


@pytest.mark.unit
def test_build_parser_exposes_all_subcommands() -> None:
    """Each subcommand must parse with its required flags; this is a smoke
    test that catches an accidentally-removed sub_parser registration."""
    cases = [
        ["start", "--csv", "./x.csv", "--triggered-by", "t"],
        ["dry-run", "--csv", "./x.csv", "--triggered-by", "t"],
        ["list", "--run-id", "1"],
        ["approve", "--diff-id", "1", "--approved-by", "t"],
        ["skip", "--diff-id", "1", "--approved-by", "t"],
        ["finalize", "--run-id", "1", "--approved-by", "t", "--notes", "x"],
        ["cancel", "--run-id", "1", "--cancelled-by", "t"],
    ]
    for argv in cases:
        args = parse_args(argv)
        assert args.cmd == argv[0], f"failed to parse {argv}"


# ---------- finalize sentinel guard ----------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cmd_finalize_rejects_missing_sentinel() -> None:
    """The finalize handler must refuse notes that don't carry the
    verification sentinel — that's the contract the D-6 push step relies on."""
    import argparse as _argparse

    args = _argparse.Namespace(run_id=1, approved_by="sre", notes="just a regular note")
    code = await cmd_finalize(args)
    assert code == EXIT_USAGE


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cmd_finalize_accepts_sentinel_prefixed_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the sentinel is present, finalize delegates to
    ReconcileService.finalize_run via the session factory. We mock both
    so this stays a unit test."""

    class _FakeRun:
        status = "applied"
        applied_count = 1
        notes = ""

    fake_run = _FakeRun()

    class _FakeSvc:
        def __init__(self, *a, **kw):  # type: ignore[no-untyped-def]
            pass

        async def finalize_run(self, *, run_id, approved_by):  # type: ignore[no-untyped-def]
            return fake_run

    class _FakeSession:
        def begin(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def flush(self):
            return None

    def _factory():
        return _FakeSession()

    monkeypatch.setattr("reconcile_admin.ReconcileService", _FakeSvc)
    monkeypatch.setattr("reconcile_admin.async_session_factory", _factory)

    import argparse as _argparse

    args = _argparse.Namespace(
        run_id=1,
        approved_by="sre",
        notes=f"{VERIFICATION_NOTES_PREFIX}2026-06-16",
    )
    code = await cmd_finalize(args)
    assert code == EXIT_OK
    assert fake_run.notes == f"{VERIFICATION_NOTES_PREFIX}2026-06-16"


# ---------- main() entry plumbing ----------


@pytest.mark.unit
def test_main_usage_error_returns_2_when_no_args() -> None:
    saved = sys.stderr
    try:
        sys.stderr = io.StringIO()
        code = main([])
    finally:
        sys.stderr = saved
    assert code == 2


@pytest.mark.unit
def test_main_dispatches_to_handler() -> None:
    async def fake_list(args):  # type: ignore[no-untyped-def]
        return EXIT_OK

    with patch.dict(
        "reconcile_admin._DISPATCH",
        {"list": fake_list},
        clear=False,
    ):
        code = main(["list", "--run-id", "1"])
    assert code == EXIT_OK


@pytest.mark.unit
def test_main_returns_failed_on_unexpected_exception() -> None:
    async def boom(args):  # type: ignore[no-untyped-def]
        raise RuntimeError("db down")

    with patch.dict(
        "reconcile_admin._DISPATCH",
        {"list": boom},
        clear=False,
    ):
        code = main(["list", "--run-id", "1"])
    assert code == EXIT_FAILED
