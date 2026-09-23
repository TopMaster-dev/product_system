"""Applying one order line to stock — the fan-out every caller must share.

There are three places an order line becomes an inventory event: first
ingestion, resolving a mapping alert in the admin screen, and the re-resolve
CLI. They used to each write the event themselves, and two of them skipped
`resolve_consumption`. That is invisible at the time and wrong afterwards:

  * a 共有在庫 parent decremented itself instead of the shared pool, so the
    sale never reached the stock the other lengths share, and
  * a ギフトバッグ (在庫管理対象外) got an event that ingestion refuses to
    write, which a later cancellation then credits back as real stock.

So the fan-out lives in one method, and the last test here is the one that
keeps it that way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.inventory import EventSource, InventoryService, LineApplication

pytestmark = pytest.mark.unit

SOURCE = EventSource(channel="rakuten", order_id="R-1", line_id="L-1")


class _Inventory(InventoryService):
    """The real `consume_order_line` / `return_order_line` over stubbed
    primitives. Only the two calls they make are replaced — the fan-out
    arithmetic under test is the shipped code."""

    def __init__(
        self,
        targets: list[tuple[int, int]],
        *,
        already_applied: bool = False,
        was_consumed: bool = True,
    ) -> None:
        self._targets = targets
        self._already_applied = already_applied
        self._was_consumed_answer = was_consumed
        self.consumed: list[tuple[int, int]] = []
        self.cancelled: list[tuple[int, int]] = []

    async def resolve_consumption(self, master_sku_id: int) -> list[tuple[int, int]]:
        return self._targets

    async def _was_consumed(self, *, master_sku_id: int, source: Any) -> bool:
        return self._was_consumed_answer

    async def consume_for_order_line(
        self, *, master_sku_id: int, quantity: int, source: Any, occurred_at: Any = None
    ) -> object | None:
        self.consumed.append((master_sku_id, quantity))
        return None if self._already_applied else object()

    async def cancel_order_line(
        self, *, master_sku_id: int, quantity: int, source: Any, occurred_at: Any = None
    ) -> object | None:
        self.cancelled.append((master_sku_id, quantity))
        return None if self._already_applied else object()


async def test_a_normal_sku_consumes_itself() -> None:
    inv = _Inventory([(42, 1)])
    result = await inv.consume_order_line(master_sku_id=42, quantity=3, source=SOURCE)
    assert inv.consumed == [(42, 3)]
    assert result == LineApplication(targets=1, events=1)


async def test_a_shared_stock_parent_moves_its_components_not_itself() -> None:
    """N108 42/45/53cm share one pool. Decrementing the parent would leave the
    pool untouched and the other two lengths still advertised as available."""
    inv = _Inventory([(101, 1), (102, 2)])
    result = await inv.consume_order_line(master_sku_id=9, quantity=4, source=SOURCE)
    assert (9, 4) not in inv.consumed
    assert inv.consumed == [(101, 4), (102, 8)]
    assert result.events == 2


async def test_an_unmanaged_master_writes_nothing_and_says_so() -> None:
    """ギフトバッグ has no stock to take. The caller needs to tell this apart
    from a failure, so it is reported rather than silently skipped."""
    inv = _Inventory([])
    result = await inv.consume_order_line(master_sku_id=7, quantity=1, source=SOURCE)
    assert inv.consumed == []
    assert result.unmanaged is True
    assert result.applied is False


async def test_replaying_an_already_applied_line_is_a_no_op_not_a_failure() -> None:
    """The source UNIQUE absorbs the second attempt. `applied` has to be False
    so the CLI does not report stock it did not move."""
    inv = _Inventory([(42, 1)], already_applied=True)
    result = await inv.consume_order_line(master_sku_id=42, quantity=3, source=SOURCE)
    assert result.targets == 1
    assert result.events == 0
    assert result.applied is False
    assert result.unmanaged is False


async def test_the_return_path_expands_exactly_like_the_consume_path() -> None:
    """An expansion that stopped on the way out must stop on the way back, or
    the cancellation credits stock the order never took."""
    consume = _Inventory([(101, 1), (102, 2)])
    give_back = _Inventory([(101, 1), (102, 2)])
    await consume.consume_order_line(master_sku_id=9, quantity=4, source=SOURCE)
    await give_back.return_order_line(master_sku_id=9, quantity=4, source=SOURCE)
    assert give_back.cancelled == consume.consumed


async def test_an_unmanaged_master_is_not_credited_back_either() -> None:
    inv = _Inventory([])
    result = await inv.return_order_line(master_sku_id=7, quantity=1, source=SOURCE)
    assert inv.cancelled == []
    assert result.unmanaged is True


async def test_a_line_that_was_never_consumed_is_not_credited() -> None:
    """`reresolve_order_items` fills the master SKU in on historical lines
    without moving stock, on purpose. A backdated cancellation on one of those
    would otherwise invent stock that was never taken."""
    inv = _Inventory([(42, 1)], was_consumed=False)
    result = await inv.return_order_line(master_sku_id=42, quantity=3, source=SOURCE)
    assert inv.cancelled == []
    assert result.applied is False
    # Not the 在庫管理対象外 case — the SKU is managed, its consumption simply
    # is not on record. The two must stay distinguishable.
    assert result.unmanaged is False


def test_no_caller_outside_the_inventory_service_writes_a_line_event() -> None:
    """The structural guard. `consume_for_order_line` and `cancel_order_line`
    are primitives: they take a master id and write an event for exactly that
    id, with no bundle expansion and no 在庫管理対象外 check. Calling one from
    outside is how both bugs above happened, and it reads as correct code.
    """
    primitives = ("consume_for_order_line", "cancel_order_line")
    owner = Path("app/services/inventory.py").resolve()

    offenders: list[str] = []
    for path in sorted(Path("app").rglob("*.py")):
        if path.resolve() == owner:
            continue
        text = path.read_text(encoding="utf-8")
        # The call shape, not the bare name — prose about these may mention them.
        offenders += [f"{path.as_posix()} -> {n}" for n in primitives if f".{n}(" in text]

    assert not offenders, (
        "これらは consume_order_line / return_order_line を使ってください: " + ", ".join(offenders)
    )
