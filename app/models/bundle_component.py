"""BundleComponent — the master↔master bill-of-materials for set/組み合わせ and
shared-stock (共有在庫) products.

Both client-requested patterns are the same topology and share this one table:

- **Set/bundle** (N21 = N23 + N32): one *bundle* master consumes N *component*
  masters, `quantity_per` each. Availability is derived, not stored.
- **Shared-stock** (anklet/bracelet, #B09): the shared physical stock is modelled
  as a *component* master (the anklet's), and each sellable SKU (anklet, bracelet)
  is a *bundle* master consuming `quantity_per=1` of it — so both display the
  same number and either sale draws the one pool.

A bundle master holds no authoritative snapshot; its availability is
`max(0, min over its components of floor(component.on_hand / quantity_per))`.
`channel_sku_mappings` (channel↔master) cannot express this master↔master
composition, hence a dedicated table.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class BundleComponent(Base, TimestampMixin):
    __tablename__ = "bundle_components"
    __table_args__ = (
        UniqueConstraint(
            "bundle_master_sku_id",
            "component_master_sku_id",
            name="uq_bundle_component",
        ),
        # Reverse lookup: given a component that just changed, find every bundle
        # to re-derive & re-push (shared N23 -> 4 sets; an anklet -> anklet+bracelet).
        Index("ix_bundle_components_component", "component_master_sku_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bundle_master_sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    component_master_sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity_per: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1", default=1
    )
