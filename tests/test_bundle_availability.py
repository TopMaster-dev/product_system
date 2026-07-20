"""Unit tests for bundle/shared-stock availability math (pure)."""

from __future__ import annotations

import pytest

from app.services.inventory import compute_bundle_available


@pytest.mark.unit
@pytest.mark.parametrize(
    ("components", "expected"),
    [
        ([(27, 1), (55, 1)], 27),  # min over components (N21 gold: N23 27, N32 55)
        ([(7, 1), (50, 1)], 7),  # N21 silver
        ([(64, 1), (86, 1)], 64),  # N29 gold: N26 binds
        ([(0, 1), (5, 1)], 0),  # a component at 0 -> set unpurchasable
        ([(-8, 1), (5, 1)], 0),  # a negative component clamps to 0 (never advertise negative)
        ([(10, 2), (10, 1)], 5),  # quantity_per=2 -> floor(10/2)=5
        ([(41, 1)], 41),  # single component (shared-stock: bracelet -> anklet)
        ([], 0),  # no components
    ],
)
def test_compute_bundle_available(components: list[tuple[int, int]], expected: int) -> None:
    assert compute_bundle_available(components) == expected
