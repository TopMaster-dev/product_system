"""Assert the alembic-migrated schema matches the ORM metadata.

Run in CI against a database that has just had `alembic upgrade head` applied.

Why this exists: `tests/conftest.py` builds the test schema with
`Base.metadata.create_all()`, so the whole suite can pass while the migrations
say something different. That is how Phase 1-B reached production with a schema
two revisions behind the code. This closes the gap by diffing what the
migrations actually produced against what the models declare.

Compared per table: column names, nullability, and index names. Types and
server-default spellings are deliberately NOT compared — they differ harmlessly
between the two paths and would only generate noise.

    DATABASE_URL_SYNC=postgresql+psycopg2://... python scripts/check_migration_parity.py
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine, inspect

from app.models import Base

# Alembic's own bookkeeping table is not part of the ORM metadata.
IGNORED_TABLES = {"alembic_version"}


def main() -> int:
    url = os.environ.get("DATABASE_URL_SYNC")
    if not url:
        print("DATABASE_URL_SYNC is not set", file=sys.stderr)
        return 2

    engine = create_engine(url)
    problems: list[str] = []
    try:
        insp = inspect(engine)
        db_tables = set(insp.get_table_names()) - IGNORED_TABLES
        orm_tables = set(Base.metadata.tables)

        for missing in sorted(orm_tables - db_tables):
            problems.append(f"{missing}: declared in the ORM but NOT created by any migration")
        for extra in sorted(db_tables - orm_tables):
            problems.append(f"{extra}: exists in the DB but is absent from the ORM")

        for name in sorted(orm_tables & db_tables):
            orm_table = Base.metadata.tables[name]

            db_cols = {c["name"]: c for c in insp.get_columns(name)}
            orm_cols = {c.name: c for c in orm_table.columns}
            for missing in sorted(set(orm_cols) - set(db_cols)):
                problems.append(f"{name}.{missing}: in the ORM but NOT added by any migration")
            for extra in sorted(set(db_cols) - set(orm_cols)):
                problems.append(f"{name}.{extra}: in the DB but not in the ORM")
            for col in sorted(set(orm_cols) & set(db_cols)):
                if bool(db_cols[col]["nullable"]) != bool(orm_cols[col].nullable):
                    problems.append(
                        f"{name}.{col}: nullable mismatch "
                        f"(DB={db_cols[col]['nullable']}, ORM={orm_cols[col].nullable})"
                    )

            db_idx = {i["name"] for i in insp.get_indexes(name)}
            orm_idx = {i.name for i in orm_table.indexes}
            for missing in sorted(orm_idx - db_idx):
                problems.append(f"{name}: index {missing} declared in the ORM but not migrated")
    finally:
        engine.dispose()

    if problems:
        print("Migration/ORM parity FAILED:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("Migration/ORM parity OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
