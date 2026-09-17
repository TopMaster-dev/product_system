"""共有在庫 linking — several SKUs drawing on one pool of physical stock.

The N108 case: only the 53cm necklace is bought, and the 42cm and 45cm listings
are the same chain cut shorter. Three variants, one pool.

The dangerous direction is linking a SKU that already holds stock. A share's own
snapshot stops being consulted once it becomes a bundle master — availability is
derived from the pool — so linking one with stock on the shelf makes that stock
vanish from every screen while the physical items still exist. Most of this file
is about refusing that, and refusing it loudly enough that the operator knows the
run did nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.cli.link_shared_stock import plan

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Answers `plan`'s reads in order: masters, snapshots, bundle_components.

    `plan` returns early when the pool itself is unusable, so the later answers
    simply go unread.
    """

    def __init__(
        self,
        masters: list[tuple[int, str, bool]],
        stock: list[tuple[int, int]] | None = None,
        components: list[tuple[int, int]] | None = None,
    ) -> None:
        self._answers: list[list[Any]] = [masters, stock or [], components or []]

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


POOL = (1, "N108gold", False)
SHARE_42 = (2, "N108gold42", False)
SHARE_45 = (3, "N108gold45", False)


async def test_new_zero_stock_shares_are_linkable() -> None:
    result = await plan(
        _FakeSession([POOL, SHARE_42, SHARE_45]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42", "N108gold45"],
    )
    assert result.pool_id == 1
    assert result.blockers == []
    assert [x.sku for x in result.ready()] == ["N108gold42", "N108gold45"]


async def test_a_share_holding_stock_is_refused_with_the_quantity() -> None:
    """Linking it would hide real stock. The quantity is in the message because
    "4 SKUs were skipped" is not something an operator can act on — whether it is
    a leftover 2 or a real 120 decides what they do next."""
    result = await plan(
        _FakeSession([POOL, SHARE_42], stock=[(2, 37)]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42"],
    )
    assert result.ready() == []
    assert len(result.blockers) == 1
    assert "37" in result.blockers[0].reason


async def test_a_zero_snapshot_is_not_stock() -> None:
    """An existing snapshot row at 0 is the normal state for a SKU that has sold
    down. It must not be confused with holding stock."""
    result = await plan(
        _FakeSession([POOL, SHARE_42], stock=[(2, 0)]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42"],
    )
    assert [x.sku for x in result.ready()] == ["N108gold42"]


async def test_re_running_reports_the_link_as_done_rather_than_missing() -> None:
    """A second run must not look like a failure, and must not insert a
    duplicate — uq_bundle_component would reject it anyway."""
    result = await plan(
        _FakeSession([POOL, SHARE_42], components=[(2, 1)]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42"],
    )
    assert result.blockers == []
    assert result.ready() == []
    assert [(x.sku, x.already_linked) for x in result.links] == [("N108gold42", True)]


async def test_a_share_already_drawing_on_a_different_pool_is_refused() -> None:
    """Silently repointing it would move stock away from whatever it was
    sharing with."""
    result = await plan(
        _FakeSession([POOL, SHARE_42], components=[(2, 99)]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42"],
    )
    assert result.ready() == []
    assert len(result.blockers) == 1


async def test_a_pool_that_is_itself_a_share_is_refused() -> None:
    """No nested sets, per the Phase 1-B design. A derived pool holds no stock of
    its own, so every share would compute its availability against nothing."""
    result = await plan(
        _FakeSession([(1, "N108gold", True), SHARE_42]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42"],
    )
    assert result.pool_id is None
    assert result.ready() == []


async def test_a_missing_pool_stops_everything() -> None:
    result = await plan(
        _FakeSession([SHARE_42]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42"],
    )
    assert result.pool_id is None
    assert [b.sku for b in result.blockers] == ["N108gold"]


async def test_a_missing_share_is_reported_and_the_others_still_plan() -> None:
    """Every problem is reported in one pass. Surfacing them one run at a time
    makes each repeat attempt look like a fresh failure."""
    result = await plan(
        _FakeSession([POOL, SHARE_42]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold42", "N108goldTYPO"],
    )
    assert [x.sku for x in result.ready()] == ["N108gold42"]
    assert [b.sku for b in result.blockers] == ["N108goldTYPO"]


async def test_a_sku_cannot_share_with_itself() -> None:
    result = await plan(
        _FakeSession([POOL]),  # type: ignore[arg-type]
        "N108gold",
        ["N108gold"],
    )
    assert result.ready() == []
    assert [b.sku for b in result.blockers] == ["N108gold"]
