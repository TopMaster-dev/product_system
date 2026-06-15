"""Verifies the ChannelAdapter ABC contract.

Concrete adapter tests land in Sprint 2.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.adapters import ChannelAdapter, NormalizedOrder, NormalizedOrderLine


@pytest.mark.unit
def test_channel_adapter_cannot_be_instantiated_directly() -> None:
    """ABC must reject direct instantiation."""
    with pytest.raises(TypeError):
        ChannelAdapter()  # type: ignore[abstract]


@pytest.mark.unit
def test_normalized_order_round_trips() -> None:
    """NormalizedOrder serializes losslessly via pydantic."""
    order = NormalizedOrder(
        channel="shopify",
        channel_order_id="ORDER-123",
        status="confirmed",
        ordered_at=datetime(2026, 5, 11, 12, 0, 0),
        items=[
            NormalizedOrderLine(
                line_id="1",
                channel_sku="10087goldS",
                quantity=2,
                unit_price=Decimal("3000"),
            ),
        ],
    )
    restored = NormalizedOrder.model_validate(order.model_dump())
    assert restored == order


@pytest.mark.unit
def test_order_line_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError):
        NormalizedOrderLine(
            line_id="1",
            channel_sku="X",
            quantity=0,
            unit_price=Decimal("100"),
        )
