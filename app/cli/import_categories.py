"""Assign SKUs to categories from a CSV, from the command line.

The same planning logic the admin screen uses (`app.services.categories.
plan_assignments`), so the two can never disagree about what a file means. The
screen exists for the client; this exists for us — when the client emails the
sheet instead of uploading it, or when a 650-row import is easier to run against
prod through the proxy than through a browser session.

CSV: `sku_code,category_code` (Japanese header aliases accepted, UTF-8 or CP932).
A BLANK category_code un-assigns that SKU — a correction, not an error.

Idempotent: rows already in their target category are counted as unchanged and
not rewritten, so re-running a corrected file is safe.

Usage (via the Cloud SQL proxy):
    py -m app.cli.import_categories --csv sku_categories.csv --dry-run
    py -m app.cli.import_categories --csv sku_categories.csv
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku
from app.services.categories import plan_assignments

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


async def run(
    *,
    csv_path: pathlib.Path,
    dry_run: bool,
    session_factory: SessionFactory | None = None,
) -> int:
    # Read before the event loop does anything else. A local file read on a CLI
    # is not the blocking hazard the async lint rule guards against, and adding
    # an async filesystem dependency for one call would be worse.
    data = csv_path.read_bytes()  # noqa: ASYNC240
    factory = session_factory or async_session_factory

    async with factory() as session, session.begin():
        inspection, plan = await plan_assignments(session, data)

        if inspection.fatal:
            log.error("import_categories.rejected", reasons=inspection.fatal)
            return 2

        if not dry_run:
            for category_id, sku_ids in plan.by_category.items():
                if not sku_ids:
                    continue
                await session.execute(
                    update(MasterSku)
                    .where(MasterSku.id.in_(sku_ids))
                    .values(category_id=category_id)
                )

        log.info(
            "import_categories.dry_run" if dry_run else "import_categories.done",
            csv_rows=inspection.total_rows,
            valid_rows=inspection.valid_rows,
            assigned=plan.assigned,
            cleared=plan.cleared,
            unchanged=plan.unchanged,
            unknown_skus=len(plan.unknown_skus),
            unknown_categories=len(plan.unknown_categories),
            # Named, not just counted: "12 unknown categories" is not something
            # anyone can act on without knowing which.
            unknown_sku_examples=sorted(set(plan.unknown_skus))[:30],
            unknown_category_examples=sorted(set(plan.unknown_categories))[:30],
            row_issues=inspection.row_issues[:20],
        )
    # Non-zero when anything was skipped, so a scripted run notices.
    return 1 if (plan.unknown_skus or plan.unknown_categories or inspection.row_issues) else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Assign SKUs to categories from a CSV")
    p.add_argument("--csv", required=True, type=pathlib.Path)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(csv_path=args.csv, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
