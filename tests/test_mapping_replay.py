"""Resolving a mapping alert replays the parked lines — and only settles the
orders that are actually settled.

`resolve_alert` used to mark an order 確定 as soon as ONE of its lines was
mapped. A Rakuten order with two unknown SKUs therefore lost its second line:
still `master_sku_id IS NULL`, never consumed, and no longer visible to
anything that looks for `pending_mapping`. The CROSS MALL cutover produces
exactly this shape — many unknown SKUs at once, several per order.

These run without a database. The session is faked, but only to answer the two
queries; the settle decision under test is the shipped code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.models import OrderStatusEnum
from app.services.mapping import MappingService

pytestmark = pytest.mark.unit


class _Order:
    def __init__(self, oid: int, status: str = OrderStatusEnum.PENDING_MAPPING) -> None:
        self.id = oid
        self.channel = "rakuten"
        self.channel_order_id = f"R-{oid}"
        self.status = status
        self.ordered_at = datetime(2026, 9, 1, tzinfo=UTC)


class _Item:
    def __init__(self, order_id: int, line_id: str, quantity: int = 1) -> None:
        self.order_id = order_id
        self.line_id = line_id
        self.quantity = quantity
        self.master_sku_id: int | None = None


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Result:
        return self

    def scalar_one_or_none(self) -> Any:
        return self._rows[0] if self._rows else None


class _Session:
    """Answers the replay query, then the "what is still unmapped" query."""

    def __init__(self, parked: list[tuple[_Item, _Order]], still_unmapped: list[int]) -> None:
        self._answers = [_Result(parked), _Result(still_unmapped)]
        self.flushes = 0

    async def execute(self, _stmt: Any) -> _Result:
        return self._answers.pop(0) if self._answers else _Result([])

    async def flush(self) -> None:
        self.flushes += 1


class _Inventory:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    async def consume_order_line(
        self, *, master_sku_id: int, quantity: int, source: Any, occurred_at: Any = None
    ) -> Any:
        self.calls.append((master_sku_id, quantity))
        return None


def _service(session: _Session) -> tuple[MappingService, _Inventory]:
    service = MappingService.__new__(MappingService)
    inventory = _Inventory()
    service._session = session  # type: ignore[attr-defined]
    service._inventory = inventory  # type: ignore[attr-defined]
    return service, inventory


async def _replay(
    parked: list[tuple[_Item, _Order]], still_unmapped: list[int]
) -> tuple[int, _Inventory]:
    session = _Session(parked, still_unmapped)
    service, inventory = _service(session)
    replayed = await service._replay_pending_lines(
        channel="rakuten", channel_sku="N108-45", master_sku_id=77
    )
    return replayed, inventory


async def test_a_fully_resolved_order_is_confirmed() -> None:
    order = _Order(1)
    replayed, inventory = await _replay([(_Item(1, "L-1", 2), order)], still_unmapped=[])
    assert replayed == 1
    assert inventory.calls == [(77, 2)]
    assert order.status == OrderStatusEnum.CONFIRMED


async def test_an_order_with_another_unknown_sku_stays_pending() -> None:
    """The bug. Confirming here strands the other line permanently."""
    order = _Order(1)
    replayed, _ = await _replay([(_Item(1, "L-1"), order)], still_unmapped=[1])
    assert replayed == 1
    assert order.status == OrderStatusEnum.PENDING_MAPPING


async def test_orders_are_judged_one_by_one() -> None:
    """A batch resolution settles one order and not the other; neither result
    may leak onto the other."""
    settled, blocked = _Order(1), _Order(2)
    parked = [(_Item(1, "L-1"), settled), (_Item(2, "L-1"), blocked)]
    replayed, _ = await _replay(parked, still_unmapped=[2])
    assert replayed == 2
    assert settled.status == OrderStatusEnum.CONFIRMED
    assert blocked.status == OrderStatusEnum.PENDING_MAPPING


async def test_the_line_is_stamped_with_the_master_sku() -> None:
    item = _Item(1, "L-1")
    session = _Session([(item, _Order(1))], still_unmapped=[])
    service, _ = _service(session)
    await service._replay_pending_lines(channel="rakuten", channel_sku="N108-45", master_sku_id=77)
    assert item.master_sku_id == 77


async def test_nothing_parked_means_no_writes_at_all() -> None:
    session = _Session([], still_unmapped=[])
    service, inventory = _service(session)
    replayed = await service._replay_pending_lines(
        channel="rakuten", channel_sku="GONE", master_sku_id=77
    )
    assert replayed == 0
    assert inventory.calls == []
    assert session.flushes == 0


async def test_the_replay_consumes_through_the_fanning_method() -> None:
    """Not `consume_for_order_line`. A 共有在庫 parent has to reach its pool,
    and a 在庫管理対象外 master has to write nothing."""
    session = _Session([(_Item(1, "L-1", 3), _Order(1))], still_unmapped=[])
    service, inventory = _service(session)
    await service._replay_pending_lines(channel="rakuten", channel_sku="N108-45", master_sku_id=77)
    assert inventory.calls == [(77, 3)]


# --- 空欄キーの拒否 --------------------------------------------------------


async def test_resolving_a_blank_channel_sku_is_refused() -> None:
    """The admin screen's alert for a blank key carries one 商品名 and hides
    the rest. Accepting it maps every one of them onto that single master."""
    from app.services.exceptions import AmbiguousChannelSkuError

    session = _Session([], still_unmapped=[])
    service, _ = _service(session)

    with pytest.raises(AmbiguousChannelSkuError):
        await service.resolve_alert(channel="shopify", channel_sku="", master_sku_id=77)


async def test_a_whitespace_channel_sku_is_refused_too() -> None:
    from app.services.exceptions import AmbiguousChannelSkuError

    session = _Session([], still_unmapped=[])
    service, _ = _service(session)

    with pytest.raises(AmbiguousChannelSkuError):
        await service.resolve_alert(channel="shopify", channel_sku="  ", master_sku_id=77)


async def test_the_refusal_happens_before_anything_is_written() -> None:
    """Otherwise a half-made mapping survives the rejection."""
    from app.services.exceptions import AmbiguousChannelSkuError

    session = _Session([], still_unmapped=[])
    service, inventory = _service(session)

    with pytest.raises(AmbiguousChannelSkuError):
        await service.resolve_alert(channel="shopify", channel_sku="", master_sku_id=77)

    assert session.flushes == 0
    assert inventory.calls == []
