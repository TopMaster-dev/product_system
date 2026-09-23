"""Read-only: which stock events should never have been written?

Two replay paths — resolving a mapping alert in the admin screen, and the old
reprocess CLI — wrote `order_consumed` without going through the bundle
fan-out or the 在庫管理対象外 check. The code is fixed; the rows it already
wrote are not, and nothing about them looks wrong from the outside. This counts
them, so the repair can be decided on numbers instead of on a guess.

WHAT IT LOOKS FOR

**在庫管理対象外のマスタに書かれた受注イベント.** ギフトバッグやクーポンは
在庫を持たないので、取込は一切イベントを書かない。アラート解決の経路だけが
書いていた。その分だけ在庫がマイナスに振れている。

ただし在庫管理対象外の指定は遡及しない。フラグを立てる前の通常の受注履歴は
そのまま残り、多くは棚卸で既に在庫0へ戻っている。初回の本番実行がまさに
それで、ギフトラッピング2SKUの2027件は全て2026-08-20までの正常な履歴だった。
そのため出力は「在庫0に戻っていないもの = 要対応」と「決着済み = 参考」を
分けて表示する。混ぜると、対応不要の2000件が深刻な指摘に見える。

**構成品に展開されなかった親のイベント.** 共有在庫/セットの親が自分自身を
減らし、共有プールは動いていない。判定は「同じ受注明細に対する構成品の
イベントが存在しないこと」— 展開されていれば必ず同じ source で残る。

WHAT IT DOES NOT LOOK FOR

スナップショットとイベント合計の乖離は `recompute_snapshots --all --dry-run`
が既に見ている。ここはイベント自体の正しさだけを見る。

Reads nothing but our own database. Writes nothing.

    powershell -File scripts/run_cli.ps1 -Cli inspect_stock_event_damage
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    BundleComponent,
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
)

log = get_logger(__name__)

#: Only order-driven events can be misrouted; a manual adjustment names its
#: master deliberately and carries no source to fan out.
_ORDER_EVENTS = (
    InventoryEventTypeEnum.ORDER_CONSUMED,
    InventoryEventTypeEnum.CANCELLATION_RETURNED,
)

_MAX_ROWS = 40


@dataclass(frozen=True, slots=True)
class DamagedSku:
    master_sku_id: int
    sku_code: str
    name: str
    note: str
    events: int
    net_delta: int
    on_hand_qty: int
    first_at: datetime | None = None
    last_at: datetime | None = None

    @property
    def settled(self) -> bool:
        """Is this SKU's stock already back where it belongs?

        The first production run made the distinction unavoidable. H1
        (ギフトラッピング) carried 1965 order events summing to -2043 — and a
        stocktake that put it back to 0 on 2026-08-20, after which nothing was
        written to it again. Those events are the ordinary history of a SKU
        that was only later marked 在庫管理対象外; the flag is not retroactive
        and the cleanup already happened.

        Reporting that as damage is how an inspection gets ignored. What is
        actually outstanding is an unmanaged SKU still holding a figure.
        """
        return self.on_hand_qty == 0

    @property
    def period(self) -> str:
        if self.first_at is None or self.last_at is None:
            return ""
        return f"{self.first_at:%Y-%m-%d} 〜 {self.last_at:%Y-%m-%d}"


def unmanaged_events_stmt() -> Select[Any]:
    """Order events against a master that is not stock-managed at all.

    Built separately from its execution so the SQL can be compiled and asserted
    without a database. A query with a wrong join returns no rows, and no rows
    reads as "nothing is broken" — the one answer this must never give by
    accident.
    """
    return (
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.non_inventory_kind,
            func.count(InventoryEvent.id),
            func.sum(InventoryEvent.quantity_delta),
            InventorySnapshot.on_hand_qty,
            func.min(InventoryEvent.occurred_at),
            func.max(InventoryEvent.occurred_at),
        )
        .join(InventoryEvent, InventoryEvent.master_sku_id == MasterSku.id)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .where(
            MasterSku.is_stock_managed.is_(False),
            InventoryEvent.event_type.in_(_ORDER_EVENTS),
        )
        .group_by(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.non_inventory_kind,
            InventorySnapshot.on_hand_qty,
        )
        .order_by(func.count(InventoryEvent.id).desc())
    )


async def find_unmanaged_events(session: AsyncSession) -> list[DamagedSku]:
    rows = (await session.execute(unmanaged_events_stmt())).all()
    return [
        DamagedSku(
            master_sku_id=mid,
            sku_code=code,
            name=name,
            note=kind or "在庫管理対象外",
            events=events,
            net_delta=net or 0,
            on_hand_qty=on_hand or 0,
            first_at=first_at,
            last_at=last_at,
        )
        for mid, code, name, kind, events, net, on_hand, first_at, last_at in rows
    ]


def unfanned_parent_events_stmt() -> Select[Any]:
    """Parent events whose order line never reached the components.

    A correct fan-out leaves a component event carrying the SAME source, so the
    absence of one is the evidence. Matching on the source rather than on dates
    keeps it exact: one order line either expanded or it did not.
    """
    component_event = aliased(InventoryEvent)
    fanned_out = (
        select(1)
        .select_from(component_event)
        .join(
            BundleComponent,
            BundleComponent.component_master_sku_id == component_event.master_sku_id,
        )
        .where(
            BundleComponent.bundle_master_sku_id == InventoryEvent.master_sku_id,
            component_event.event_type == InventoryEvent.event_type,
            component_event.source_channel == InventoryEvent.source_channel,
            component_event.source_order_id == InventoryEvent.source_order_id,
            component_event.source_line_id == InventoryEvent.source_line_id,
        )
        .exists()
    )
    has_components = (
        select(1)
        .select_from(BundleComponent)
        .where(BundleComponent.bundle_master_sku_id == InventoryEvent.master_sku_id)
        .exists()
    )

    return (
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            func.count(InventoryEvent.id),
            func.sum(InventoryEvent.quantity_delta),
            InventorySnapshot.on_hand_qty,
            func.min(InventoryEvent.occurred_at),
            func.max(InventoryEvent.occurred_at),
        )
        .join(InventoryEvent, InventoryEvent.master_sku_id == MasterSku.id)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .where(
            InventoryEvent.event_type.in_(_ORDER_EVENTS),
            InventoryEvent.source_order_id.is_not(None),
            has_components,
            ~fanned_out,
        )
        .group_by(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            InventorySnapshot.on_hand_qty,
        )
        .order_by(func.count(InventoryEvent.id).desc())
    )


async def find_unfanned_parent_events(session: AsyncSession) -> list[DamagedSku]:
    rows = (await session.execute(unfanned_parent_events_stmt())).all()
    return [
        DamagedSku(
            master_sku_id=mid,
            sku_code=code,
            name=name,
            note="共有在庫/セットの親",
            events=events,
            net_delta=net or 0,
            on_hand_qty=on_hand or 0,
            first_at=first_at,
            last_at=last_at,
        )
        for mid, code, name, events, net, on_hand, first_at, last_at in rows
    ]


def _print_rows(rows: list[DamagedSku]) -> None:
    print(f"\n    {'SKU':<20}{'区分':<14}{'件数':>6}{'影響':>9}{'現在庫':>8}  {'期間':<26}商品名")
    for row in rows[:_MAX_ROWS]:
        print(
            f"    {row.sku_code:<20}{row.note:<14}{row.events:>6}{row.net_delta:>+9}"
            f"{row.on_hand_qty:>8}  {row.period:<26}{row.name[:24]}"
        )
    if len(rows) > _MAX_ROWS:
        print(f"    ... ほか {len(rows) - _MAX_ROWS}件")


def _print_section(title: str, explanation: str, damaged: list[DamagedSku]) -> None:
    """Outstanding first, settled history second, and never mixed.

    The first production run returned 2027 events across two gift-wrap SKUs,
    every one of them ordinary pre-flag history already zeroed by a stocktake.
    Printed as one list it reads as a serious finding, and the next person to
    run this during 検収 would have chased it. Split, the same data says
    「対応不要」on its own.
    """
    outstanding = [d for d in damaged if not d.settled]
    settled = [d for d in damaged if d.settled]

    print(f"\n  === {title} ===")
    if not outstanding:
        print("  要対応なし")
    else:
        print(f"  {explanation}")
        _print_rows(outstanding)
        total = sum(r.net_delta for r in outstanding)
        print(f"\n    要対応 {len(outstanding)}SKU / 在庫のずれ {total:+}")

    if settled:
        print(
            f"\n  参考: 在庫0で決着済みのSKUが {len(settled)}件あります — 対応不要。"
            "\n  フラグを立てる前の通常の受注履歴です。在庫管理対象外の指定は遡及しないため、"
            "\n  過去のイベントはそのまま残り、棚卸や手動調整で既に決着しています。"
        )
        _print_rows(settled)


async def run() -> int:
    async with async_session_factory() as session:
        unmanaged = await find_unmanaged_events(session)
        unfanned = await find_unfanned_parent_events(session)

    print("\n  書かれてはいけない在庫イベントの調査 — 読み取りのみ")

    _print_section(
        "在庫管理対象外のマスタに書かれた受注イベント",
        "ギフトバッグ等は取込時なら一切イベントを書きません。"
        "在庫0に戻っていないものが残っています。",
        unmanaged,
    )
    _print_section(
        "構成品に展開されなかった親のイベント",
        "親だけが増減し、共有プールは動いていません。"
        "同じ受注明細に対する構成品のイベントが存在しないものだけを挙げています。",
        unfanned,
    )

    outstanding = [d for d in unmanaged + unfanned if not d.settled]
    if not outstanding:
        print("\n  対応が必要なイベントはありません")
    else:
        print(
            "\n  ※ この一覧は調査のみで、何も変更していません。"
            "\n     訂正は手動調整 /admin/adjust で行うか、方針を決めたうえで実施してください。"
            "\n     スナップショットとイベント合計の乖離は別途:"
            "\n       powershell -File scripts/run_cli.ps1 -Cli recompute_snapshots"
            ' -Args "--all --dry-run"'
        )

    log.info(
        "stock_event_damage.done",
        # Outstanding is the number worth alerting on; the totals are context.
        outstanding_skus=len(outstanding),
        outstanding_delta=sum(r.net_delta for r in outstanding),
        unmanaged_skus=len(unmanaged),
        unmanaged_events=sum(r.events for r in unmanaged),
        unfanned_skus=len(unfanned),
        unfanned_events=sum(r.events for r in unfanned),
    )
    return 0


def main() -> None:
    argparse.ArgumentParser(
        description="Report order events that should never have been written"
    ).parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
