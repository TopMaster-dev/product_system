"""Cloud Run Job entry for F1.7 ReconcileService administration.

This is the human-driven counterpart to the daily Cloud Scheduler CLI
(`app/cli/reconcile_inventory.py`). It exposes each ReconcileService
operation as a sub-command so an SRE can drive the verification flow
(docs/13 §F1.7) end-to-end without writing a custom one-off script
each time.

Sub-commands map 1:1 to ReconcileService methods:

  start      Wrap reconcile_inventory.run() — read a CSV, create a
             ReconcileRun in pending_approval status.
  list       List pending diffs for a run (queues the operator's work).
  approve    Approve one diff (generates a stocktake event).
  skip       Skip one diff (no event, no snapshot change).
  finalize   Transition a run to applied. Requires all diffs decided.
             Operators MUST pass --notes containing a verification
             sentinel (e.g. VERIFICATION_DO_NOT_PUSH_F17_*) so the
             D-6 batched push step can filter the run out.
  cancel     Cancel a run with no approvals.
  dry-run    Same as start but rolls back; reports the diff summary only.

Exit codes:
  0 — operation succeeded
  1 — operation reported a logical failure (e.g. RuntimeError from the service)
  2 — usage error

Usage (Cloud Run Job):
    # --csv must be a local path inside the container. The operator stages
    # a dummy CSV with scripts/make_dummy_recon_csv.py, bundles it via the
    # image build (or GCS FUSE mount), then references the in-container path.
    gcloud run jobs execute product-system-reconcile-admin \\
        --args=start,--csv=/app/recon_dummy.csv,\\
               --triggered-by=sre-verify --wait

    gcloud run jobs execute product-system-reconcile-admin \\
        --args=approve,--diff-id=42,--approved-by=sre-verify --wait

    gcloud run jobs execute product-system-reconcile-admin \\
        --args=finalize,--run-id=7,--approved-by=sre-verify,\\
               --notes=VERIFICATION_DO_NOT_PUSH_F17_2026-06-16 --wait
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from sqlalchemy import select

from app.cli import reconcile_inventory
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ReconcileDiff
from app.services.reconcile import ReconcileService

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

# Operators MUST tag verification finalizes with this prefix so the D-6
# batched-push step can filter the run out of channel pushes.
VERIFICATION_NOTES_PREFIX = "VERIFICATION_DO_NOT_PUSH_F17_"


# ---------- sub-command handlers ----------


async def cmd_start(args: argparse.Namespace) -> int:
    """Delegates to app.cli.reconcile_inventory.run() so the CSV-loading
    and diff-collection logic stays single-sourced."""
    exit_code = await reconcile_inventory.run(
        Path(args.csv),
        triggered_by=args.triggered_by,
        dry_run=False,
    )
    return exit_code if exit_code in (0, 2) else EXIT_FAILED


async def cmd_dry_run(args: argparse.Namespace) -> int:
    exit_code = await reconcile_inventory.run(
        Path(args.csv),
        triggered_by=args.triggered_by,
        dry_run=True,
    )
    return exit_code if exit_code in (0, 2) else EXIT_FAILED


async def cmd_list(args: argparse.Namespace) -> int:
    """List the diffs for a run with their current decision."""
    async with async_session_factory() as session:
        result = await session.execute(
            select(ReconcileDiff)
            .where(ReconcileDiff.reconcile_run_id == args.run_id)
            .order_by(ReconcileDiff.id)
        )
        diffs = list(result.scalars().all())
    sys.stdout.write(
        json.dumps(
            {
                "run_id": args.run_id,
                "diff_count": len(diffs),
                "diffs": [
                    {
                        "diff_id": d.id,
                        "master_sku_id": d.master_sku_id,
                        "current_qty": d.current_qty,
                        "target_qty": d.target_qty,
                        "delta": d.delta,
                        "decision": d.decision,
                    }
                    for d in diffs
                ],
            }
        )
        + "\n"
    )
    return EXIT_OK


async def cmd_approve(args: argparse.Namespace) -> int:
    async with async_session_factory() as session, session.begin():
        svc = ReconcileService(session)
        try:
            result = await svc.approve_diff(
                diff_id=args.diff_id,
                approved_by=args.approved_by,
            )
        except (RuntimeError, ValueError) as exc:
            log.warning(
                "reconcile_admin.approve_failed",
                diff_id=args.diff_id,
                error=str(exc),
            )
            sys.stdout.write(
                json.dumps({"diff_id": args.diff_id, "result": "error", "error": str(exc)}) + "\n"
            )
            return EXIT_FAILED
        event_id = result.event.id if result.event else None
        sys.stdout.write(
            json.dumps(
                {
                    "diff_id": args.diff_id,
                    "result": "approved",
                    "decision": result.diff.decision,
                    "applied_event_id": event_id,
                }
            )
            + "\n"
        )
    return EXIT_OK


async def cmd_skip(args: argparse.Namespace) -> int:
    async with async_session_factory() as session, session.begin():
        svc = ReconcileService(session)
        try:
            diff = await svc.skip_diff(
                diff_id=args.diff_id,
                approved_by=args.approved_by,
            )
        except (RuntimeError, ValueError) as exc:
            sys.stdout.write(
                json.dumps({"diff_id": args.diff_id, "result": "error", "error": str(exc)}) + "\n"
            )
            return EXIT_FAILED
        sys.stdout.write(
            json.dumps({"diff_id": args.diff_id, "result": "skipped", "decision": diff.decision})
            + "\n"
        )
    return EXIT_OK


async def cmd_finalize(args: argparse.Namespace) -> int:
    if not args.notes.startswith(VERIFICATION_NOTES_PREFIX):
        sys.stderr.write(
            f"error: --notes must start with {VERIFICATION_NOTES_PREFIX!r} "
            "so the D-6 batched-push step filters this run out.\n"
        )
        return EXIT_USAGE
    async with async_session_factory() as session, session.begin():
        svc = ReconcileService(session)
        try:
            run = await svc.finalize_run(run_id=args.run_id, approved_by=args.approved_by)
        except (RuntimeError, ValueError) as exc:
            sys.stdout.write(
                json.dumps({"run_id": args.run_id, "result": "error", "error": str(exc)}) + "\n"
            )
            return EXIT_FAILED
        run.notes = args.notes
        await session.flush()
    sys.stdout.write(
        json.dumps(
            {
                "run_id": args.run_id,
                "result": "finalized",
                "status": run.status,
                "applied_count": run.applied_count,
                "notes": run.notes,
            }
        )
        + "\n"
    )
    return EXIT_OK


async def cmd_cancel(args: argparse.Namespace) -> int:
    async with async_session_factory() as session, session.begin():
        svc = ReconcileService(session)
        try:
            run = await svc.cancel_run(
                run_id=args.run_id,
                cancelled_by=args.cancelled_by,
                reason=args.reason or None,
            )
        except (RuntimeError, ValueError) as exc:
            sys.stdout.write(
                json.dumps({"run_id": args.run_id, "result": "error", "error": str(exc)}) + "\n"
            )
            return EXIT_FAILED
        sys.stdout.write(
            json.dumps({"run_id": args.run_id, "result": "cancelled", "status": run.status}) + "\n"
        )
    return EXIT_OK


# ---------- argparse ----------


_DISPATCH: dict[str, Callable[[argparse.Namespace], Awaitable[int]]] = {
    "start": cmd_start,
    "dry-run": cmd_dry_run,
    "list": cmd_list,
    "approve": cmd_approve,
    "skip": cmd_skip,
    "finalize": cmd_finalize,
    "cancel": cmd_cancel,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="reconcile_admin",
        description="F1.7 ReconcileService admin sub-commands for production "
        "verification (docs/13 §F1.7).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", help="Create a ReconcileRun from a CSV.")
    s.add_argument("--csv", required=True, help="Path or gs:// URL to the CROSS MALL CSV.")
    s.add_argument("--triggered-by", required=True, help="Identifier persisted on the run.")

    d = sub.add_parser("dry-run", help="Diff-only; do not persist a ReconcileRun.")
    d.add_argument("--csv", required=True)
    d.add_argument("--triggered-by", required=True)

    list_p = sub.add_parser("list", help="List diffs for a run.")
    list_p.add_argument("--run-id", required=True, type=int)

    a = sub.add_parser("approve", help="Approve one diff (creates stocktake event).")
    a.add_argument("--diff-id", required=True, type=int)
    a.add_argument("--approved-by", required=True)

    sk = sub.add_parser("skip", help="Skip one diff.")
    sk.add_argument("--diff-id", required=True, type=int)
    sk.add_argument("--approved-by", required=True)

    f = sub.add_parser("finalize", help="Finalize a run (applies the audit-trail status change).")
    f.add_argument("--run-id", required=True, type=int)
    f.add_argument("--approved-by", required=True)
    f.add_argument(
        "--notes",
        required=True,
        help=f"Must start with {VERIFICATION_NOTES_PREFIX!r} for verification runs.",
    )

    c = sub.add_parser("cancel", help="Cancel a run with zero approvals.")
    c.add_argument("--run-id", required=True, type=int)
    c.add_argument("--cancelled-by", required=True)
    c.add_argument("--reason", default="")

    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


async def dispatch(args: argparse.Namespace) -> int:
    handler = _DISPATCH[args.cmd]
    return await handler(args)


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or EXIT_USAGE)
    try:
        return asyncio.run(dispatch(args))
    except Exception:
        log.exception("reconcile_admin.unexpected_error", cmd=getattr(args, "cmd", "?"))
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
