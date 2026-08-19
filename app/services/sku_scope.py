"""The single source of truth for "which master SKUs does this query cover?".

Every screen, job and aggregate in Phase 2 narrows the SKU population, and each
one narrows it *differently*. Writing those predicates inline is how ~32 bundle
parents and ~400 archived masters end up included in one screen and excluded from
the next — a discrepancy nobody notices until the numbers are compared.

Three flags participate, and they mean genuinely different things:

`is_bundle`
    Set/shared-stock parents. They hold NO stock of their own; availability is
    derived from `bundle_components`. Counting them in a stock or demand
    aggregate double-counts their components.

`is_stock_managed`
    False for gift boxes, coupons, made-to-order items. These "sell" with every
    order but have no physical stock, so they march negative forever (one hit
    -12509 in Phase 1-B) and they polluted the best-seller ranking. They produce
    no inventory events at all once flagged.

`archived_at`
    Retired masters — mostly the legacy product-level rows the variant cutover
    replaced. This is a VISIBILITY concept, not an inventory one, which is why it
    is handled asymmetrically:

        current-state screens/jobs  ->  exclude archived
        historical period aggregates ->  INCLUDE archived

    Filtering archived out of a period report would erase those masters' past
    sales and silently break every comparison spanning the 2026-07-20 cutover.

Because that asymmetry is the easiest thing to get wrong, `include_archived` is a
REQUIRED keyword on `analysable_conditions()`: callers must state whether they are
reporting on history or on the present.

Usage:

    stmt = select(MasterSku).where(*analysable_conditions(include_archived=True))
"""

from __future__ import annotations

from sqlalchemy import ColumnElement

from app.models import MasterSku

__all__ = ["analysable_conditions", "operational_conditions"]


def analysable_conditions(*, include_archived: bool) -> list[ColumnElement[bool]]:
    """SKU population for ANALYTICS: dashboards, velocity, forecasting, reorder.

    Always excludes bundle parents (their stock is derived from components) and
    non-stock-managed items (packaging/coupons are not merchandise).

    `include_archived` is required and has no default:

    * ``True``  — historical period aggregates (sales trends, period comparison).
      Retired masters really did sell in the past; dropping them rewrites history.
    * ``False`` — present-tense analytics (stock rollup, velocity, forecast,
      reorder). A retired SKU must not appear in "what should I order today".
    """
    conditions: list[ColumnElement[bool]] = [
        MasterSku.is_stock_managed.is_(True),
        MasterSku.is_bundle.is_(False),
    ]
    if not include_archived:
        conditions.append(MasterSku.archived_at.is_(None))
    return conditions


def operational_conditions(
    *,
    include_archived: bool = False,
    include_unmanaged: bool = False,
) -> list[ColumnElement[bool]]:
    """SKU population for CURRENT-STATE operational screens (inventory list,
    manual adjust, stocktake, alerts).

    Unlike `analysable_conditions`, bundle parents are NOT excluded: an operator
    legitimately looks up a set and sees its derived availability.

    Defaults hide retired and non-stock-managed SKUs, which is what an operator
    wants; pass the flags to widen the view (e.g. an "アーカイブ済も表示"
    toggle, or the maintenance screen that assigns `non_inventory_kind`).
    """
    conditions: list[ColumnElement[bool]] = []
    if not include_archived:
        conditions.append(MasterSku.archived_at.is_(None))
    if not include_unmanaged:
        conditions.append(MasterSku.is_stock_managed.is_(True))
    return conditions
