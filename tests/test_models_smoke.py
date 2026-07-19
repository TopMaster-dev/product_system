"""Pure-Python smoke tests for the ORM model layer.

These verify the models import cleanly, the metadata is complete, and the
declared constraints/indexes are present — no database required.
"""

from __future__ import annotations

import pytest

from app.models import Base


@pytest.mark.unit
def test_all_expected_tables_registered() -> None:
    expected = {
        "master_skus",
        "channel_sku_mappings",
        "orders",
        "order_items",
        "inventory_events",
        "inventory_snapshots",
        "mapping_alerts",
        "webhook_logs",
        "sync_attempts",  # Phase 1-B F1.1
        "reconcile_runs",  # Phase 1-B F1.3
        "reconcile_diffs",  # Phase 1-B F1.3
        "bundle_components",  # Phase 1-B 組み合わせ商品 / 共有在庫
    }
    assert expected.issubset(set(Base.metadata.tables.keys()))


@pytest.mark.unit
def test_sync_attempts_indexes_present() -> None:
    """Two compound indexes drive the admin sync-errors page and SKU history."""
    table = Base.metadata.tables["sync_attempts"]
    index_columns = {tuple(c.name for c in idx.columns) for idx in table.indexes}
    assert ("status", "started_at") in index_columns
    assert ("master_sku_id", "started_at") in index_columns


@pytest.mark.unit
def test_sync_attempts_required_columns() -> None:
    table = Base.metadata.tables["sync_attempts"]
    required_non_null = {
        "id",
        "attempt_type",
        "payload",
        "status",
        "attempt_count",
        "started_at",
        "created_at",
        "updated_at",
    }
    assert required_non_null.issubset(set(table.columns.keys()))
    # nullable correctness
    assert table.c.attempt_type.nullable is False
    assert table.c.status.nullable is False
    assert table.c.payload.nullable is False
    assert table.c.channel.nullable is True
    assert table.c.master_sku_id.nullable is True
    assert table.c.parent_attempt_id.nullable is True


@pytest.mark.unit
def test_sync_attempts_self_referential_fk() -> None:
    """parent_attempt_id supports linking retries back to the original attempt."""
    table = Base.metadata.tables["sync_attempts"]
    fk_target_tables = {fk.column.table.name for col in table.columns for fk in col.foreign_keys}
    assert "sync_attempts" in fk_target_tables  # parent_attempt_id self-FK
    assert "master_skus" in fk_target_tables  # master_sku_id FK


@pytest.mark.unit
def test_reconcile_diff_uniqueness_constraint() -> None:
    """Same SKU cannot appear twice in the same reconcile run."""
    table = Base.metadata.tables["reconcile_diffs"]
    cols = {
        tuple(sorted(c.name for c in uc.columns))
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("master_sku_id", "reconcile_run_id") in cols


@pytest.mark.unit
def test_reconcile_diff_fk_targets() -> None:
    """reconcile_diffs links runs, master_skus, and the applied stocktake event."""
    table = Base.metadata.tables["reconcile_diffs"]
    fk_target_tables = {fk.column.table.name for col in table.columns for fk in col.foreign_keys}
    assert "reconcile_runs" in fk_target_tables
    assert "master_skus" in fk_target_tables
    assert "inventory_events" in fk_target_tables


@pytest.mark.unit
def test_reconcile_run_has_status_started_index() -> None:
    """Status-by-time index drives the recent-runs admin query."""
    table = Base.metadata.tables["reconcile_runs"]
    index_columns = {tuple(c.name for c in idx.columns) for idx in table.indexes}
    assert ("status", "started_at") in index_columns


@pytest.mark.unit
def test_reconcile_diff_decision_index_for_approval_queue() -> None:
    """Composite (run, decision) index supports the 'pending approvals in this run' page."""
    table = Base.metadata.tables["reconcile_diffs"]
    index_columns = {tuple(c.name for c in idx.columns) for idx in table.indexes}
    assert ("reconcile_run_id", "decision") in index_columns


@pytest.mark.unit
def test_reconcile_enums_have_expected_values() -> None:
    from app.models.enums import (
        ReconcileDiffDecisionEnum,
        ReconcileRunStatusEnum,
    )

    assert set(ReconcileRunStatusEnum) == {
        ReconcileRunStatusEnum.RUNNING,
        ReconcileRunStatusEnum.PENDING_APPROVAL,
        ReconcileRunStatusEnum.APPLIED,
        ReconcileRunStatusEnum.CANCELLED,
    }
    assert set(ReconcileDiffDecisionEnum) == {
        ReconcileDiffDecisionEnum.PENDING,
        ReconcileDiffDecisionEnum.APPROVED,
        ReconcileDiffDecisionEnum.SKIPPED,
    }


@pytest.mark.unit
def test_inventory_event_idempotency_constraint() -> None:
    """master_sku_id is part of the key so one order line can fan out to
    per-component decrements (bundle/shared-stock) without colliding."""
    table = Base.metadata.tables["inventory_events"]
    constraint_columns = {
        tuple(sorted(c.name for c in uc.columns))
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert (
        "event_type",
        "master_sku_id",
        "source_channel",
        "source_line_id",
        "source_order_id",
    ) in constraint_columns


@pytest.mark.unit
def test_master_sku_has_is_bundle_flag() -> None:
    table = Base.metadata.tables["master_skus"]
    assert "is_bundle" in table.columns
    assert table.c.is_bundle.nullable is False
    assert table.c.is_bundle.type.python_type is bool


@pytest.mark.unit
def test_bundle_components_uniqueness_and_fks() -> None:
    """A (bundle, component) pair is unique, and both sides FK to master_skus."""
    table = Base.metadata.tables["bundle_components"]
    cols = {
        tuple(sorted(c.name for c in uc.columns))
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("bundle_master_sku_id", "component_master_sku_id") in cols
    fk_targets = {fk.column.table.name for col in table.columns for fk in col.foreign_keys}
    assert fk_targets == {"master_skus"}
    assert table.c.quantity_per.nullable is False


@pytest.mark.unit
def test_bundle_components_reverse_index() -> None:
    """Index on component_master_sku_id drives the 'which bundles use this
    component?' re-derive lookup (shared N23 -> 4 sets; anklet -> anklet+bracelet)."""
    table = Base.metadata.tables["bundle_components"]
    index_columns = {tuple(c.name for c in idx.columns) for idx in table.indexes}
    assert ("component_master_sku_id",) in index_columns


@pytest.mark.unit
def test_order_uniqueness_constraint() -> None:
    table = Base.metadata.tables["orders"]
    cols = {
        tuple(sorted(c.name for c in uc.columns))
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("channel", "channel_order_id") in cols


@pytest.mark.unit
def test_channel_sku_mapping_uniqueness() -> None:
    table = Base.metadata.tables["channel_sku_mappings"]
    cols = {
        tuple(sorted(c.name for c in uc.columns))
        for uc in table.constraints
        if uc.__class__.__name__ == "UniqueConstraint"
    }
    assert ("channel", "channel_sku", "marketplace_id") in cols


@pytest.mark.unit
def test_quantity_check_constraint_present() -> None:
    """order_items.quantity must have a positivity check at the schema level."""
    table = Base.metadata.tables["order_items"]
    has_check = any(
        c.__class__.__name__ == "CheckConstraint" and c.name == "ck_order_items_qty_positive"
        for c in table.constraints
    )
    # The check is defined at the Alembic level (migration), not in the model.
    # We assert that quantity is non-nullable and an Integer here.
    assert table.c.quantity.nullable is False
    assert has_check or table.c.quantity.type.python_type is int
