"""BigQuery schema parity gate.

The daily export loads rows with `autodetect=False` against the Terraform-pinned
schemas in `infra/terraform/bq_schemas/*.json`. If the ORM gains a column that the
JSON lacks, the load fails at runtime — and `/internal/jobs/bq-export` reports that
as HTTP 200 `{"status": "partial"}`, so the failure is silent.

This happened in production: `master_skus.is_bundle` (migration 0005) and
`master_skus.image_url` (0008) were never added to the JSON, breaking the daily
master_skus export.

These tests make that class of drift a CI failure instead of a silent prod defect.

RULE when adding a column to an ALREADY-DEPLOYED table: the new field MUST be
`"mode": "NULLABLE"`, even when the ORM column is `nullable=False`. BigQuery only
permits adding NULLABLE/REPEATED columns to an existing table, so a REQUIRED field
makes Terraform plan a destroy+create of the table — i.e. it silently proposes
deleting every exported row. `test_schema_nullability_matches_orm` therefore only
rejects the unsafe direction (JSON REQUIRED while the ORM allows NULL); JSON
NULLABLE against a NOT NULL ORM column is correct and expected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models import Base
from app.services.bigquery_export import TABLE_SPECS

pytestmark = pytest.mark.unit

_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "bq_schemas"


def _json_schema(table: str) -> list[dict[str, str]]:
    return json.loads((_SCHEMA_DIR / f"{table}.json").read_text(encoding="utf-8"))


def _orm_table(table: str):
    return Base.metadata.tables[table]


@pytest.mark.parametrize("spec", TABLE_SPECS, ids=lambda s: s.name)
def test_every_exported_table_has_a_schema_file(spec) -> None:
    assert (_SCHEMA_DIR / f"{spec.name}.json").is_file(), (
        f"{spec.name} is exported but infra/terraform/bq_schemas/{spec.name}.json is missing"
    )


@pytest.mark.parametrize("spec", TABLE_SPECS, ids=lambda s: s.name)
def test_schema_columns_match_orm(spec) -> None:
    """Column name sets must match exactly. Extra ORM columns break the load
    (autodetect=False); extra JSON columns silently stay NULL forever."""
    json_cols = {f["name"] for f in _json_schema(spec.name)}
    orm_cols = {c.name for c in _orm_table(spec.name).columns}
    missing_in_json = orm_cols - json_cols
    missing_in_orm = json_cols - orm_cols
    assert not missing_in_json, (
        f"{spec.name}: ORM columns absent from bq_schemas JSON -> the daily load FAILS: "
        f"{sorted(missing_in_json)}"
    )
    assert not missing_in_orm, (
        f"{spec.name}: JSON columns that no longer exist in the ORM: {sorted(missing_in_orm)}"
    )


@pytest.mark.parametrize("spec", TABLE_SPECS, ids=lambda s: s.name)
def test_schema_nullability_matches_orm(spec) -> None:
    """A REQUIRED BigQuery field fed a NULL fails the whole load job."""
    orm_cols = {c.name: c for c in _orm_table(spec.name).columns}
    mismatches = []
    for field in _json_schema(spec.name):
        col = orm_cols.get(field["name"])
        if col is None:
            continue  # covered by the column-parity test
        json_required = field.get("mode") == "REQUIRED"
        orm_required = not col.nullable
        if json_required and not orm_required:
            mismatches.append(f"{field['name']}: JSON=REQUIRED but ORM is nullable")
    assert not mismatches, f"{spec.name}: {mismatches}"


def test_every_spec_has_a_fetcher() -> None:
    """`_fetch_rows` dispatches on spec.name; a spec with no branch returns []
    and would silently export an empty table."""
    import inspect

    from app.services.bigquery_export import BigQueryExportService

    src = inspect.getsource(BigQueryExportService._fetch_rows)
    for spec in TABLE_SPECS:
        assert f'"{spec.name}"' in src, (
            f"TABLE_SPECS includes {spec.name} but _fetch_rows has no branch for it"
        )
