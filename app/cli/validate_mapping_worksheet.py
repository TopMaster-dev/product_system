"""Read-only: check the client's answers before any of them become a mapping.

The unmapped worksheet goes out with a blank column and comes back with a
product code in it. Those answers cannot be imported on trust, because a wrong
mapping is silent: the sale simply lands on another product for ever, and
nothing anywhere reports an error.

WHAT GOES WRONG, SPECIFICALLY

**The answer names a retired master.** The variant cutover replaced
product-level masters (`B46`) with variant-level ones (`B46gold`,
`B46silver`) and archived the originals. A person reading a product list
writes the product code, because that is what a product is called. Mapping to
it attributes the revenue to a master no screen shows any more.

**One answer covers several rows.** Rakuten carries a separate code per
colour, so two rows can legitimately share one answer — or the answer can be
one colour's code pasted onto both, which is a real error. The pair cannot be
told apart by the codes alone; it is reported so a person looks.

**The answer is a 在庫管理対象外 or 共有在庫の親 master.** Both are valid
targets for revenue but mean something specific for stock, so they are called
out rather than waved through.

Reads the CSV and the database. Writes nothing.

    powershell -File scripts/run_cli.ps1 -Cli validate_mapping_worksheet ^
        -Args "--in csv_file/phase2/source/unmapped_worksheet_Corrected.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import BundleComponent, ChannelSkuMapping, MasterSku
from app.services.sku_scope import operational_conditions

log = get_logger(__name__)

COL_CHANNEL = "チャネル"
COL_CHANNEL_SKU = "チャネルの商品コード"
COL_NAME = "商品名"
COL_AMOUNT = "金額合計"
COL_ANSWER = "正しい商品コード ご記入ください"
COL_NOTE = "備考"

#: Written in 備考 when the client has deleted the page. Not an error.
OUT_OF_SCOPE = ("対象外", "削除", "終了", "廃番")


@dataclass
class Answer:
    channel: str
    channel_sku: str
    product_name: str
    amount: int
    code: str
    note: str
    verdict: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict == "OK"


@dataclass
class MasterInfo:
    sku_code: str
    archived: bool
    unmanaged: bool
    is_bundle: bool
    has_components: bool


@dataclass
class Report:
    answers: list[Answer] = field(default_factory=list)

    def by_verdict(self) -> dict[str, list[Answer]]:
        out: dict[str, list[Answer]] = defaultdict(list)
        for a in self.answers:
            out[a.verdict].append(a)
        return out


def read_worksheet(path: Path) -> list[Answer]:
    text = path.read_text(encoding="utf-8-sig")
    rows = list(csv.DictReader(text.splitlines()))
    if not rows:
        raise ValueError(f"{path} に行がありません")
    missing = [c for c in (COL_CHANNEL, COL_CHANNEL_SKU, COL_ANSWER) if c not in rows[0]]
    if missing:
        raise ValueError(f"必要な列がありません: {missing}")

    answers = []
    for row in rows:
        amount = (row.get(COL_AMOUNT) or "0").replace(",", "").strip()
        answers.append(
            Answer(
                channel=(row.get(COL_CHANNEL) or "").strip(),
                channel_sku=(row.get(COL_CHANNEL_SKU) or "").strip(),
                product_name=(row.get(COL_NAME) or "").strip(),
                amount=int(amount) if amount.lstrip("-").isdigit() else 0,
                code=(row.get(COL_ANSWER) or "").strip(),
                note=(row.get(COL_NOTE) or "").strip(),
            )
        )
    return answers


async def load_masters(session: AsyncSession, codes: set[str]) -> dict[str, MasterInfo]:
    if not codes:
        return {}
    rows = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.archived_at,
            MasterSku.is_stock_managed,
            MasterSku.is_bundle,
        ).where(MasterSku.sku_code.in_(codes))
    )
    found = list(rows.all())
    if not found:
        return {}

    component_rows = await session.execute(
        select(BundleComponent.bundle_master_sku_id)
        .where(BundleComponent.bundle_master_sku_id.in_([r[0] for r in found]))
        .distinct()
    )
    with_components = set(component_rows.scalars().all())

    return {
        code: MasterInfo(
            sku_code=code,
            archived=archived is not None,
            unmanaged=not managed,
            is_bundle=bool(bundle),
            has_components=mid in with_components,
        )
        for mid, code, archived, managed, bundle in found
    }


async def suggest(session: AsyncSession, code: str) -> list[str]:
    """Live masters whose code starts with the answer.

    `B46` answered against `B46gold` / `B46silver` is the shape the variant
    cutover guarantees, so the prefix is the useful hint — and offering only
    live ones keeps the retired master out of the suggestion.
    """
    rows = await session.execute(
        select(MasterSku.sku_code)
        .where(
            MasterSku.sku_code.ilike(f"{code}%"),
            *operational_conditions(include_unmanaged=True),
        )
        .order_by(MasterSku.sku_code)
        .limit(8)
    )
    return [c for c in rows.scalars().all() if c != code]


async def already_mapped(
    session: AsyncSession, pairs: list[tuple[str, str]]
) -> set[tuple[str, str]]:
    if not pairs:
        return set()
    rows = await session.execute(
        select(ChannelSkuMapping.channel, ChannelSkuMapping.channel_sku).where(
            ChannelSkuMapping.is_active.is_(True)
        )
    )
    live = {(c, s) for c, s in rows.all()}
    return {p for p in pairs if p in live}


async def validate(session: AsyncSession, answers: list[Answer]) -> Report:
    masters = await load_masters(session, {a.code for a in answers if a.code})
    mapped = await already_mapped(session, [(a.channel, a.channel_sku) for a in answers])

    # Answers used on more than one row. Legitimate when a channel carries a
    # code per colour, wrong when one colour's code was pasted onto both.
    shared: dict[str, list[Answer]] = defaultdict(list)
    for a in answers:
        if a.code:
            shared[a.code].append(a)

    for a in answers:
        if any(k in a.note for k in OUT_OF_SCOPE):
            a.verdict, a.detail = "対象外", f"備考: {a.note}"
            continue
        if not a.code:
            a.verdict, a.detail = "未記入", ""
            continue
        if (a.channel, a.channel_sku) in mapped:
            a.verdict, a.detail = "既にマッピング済", "この商品コードには有効な対応表があります"
            continue

        info = masters.get(a.code)
        if info is None:
            hints = await suggest(session, a.code)
            a.verdict = "該当なし"
            a.detail = f"候補: {', '.join(hints)}" if hints else "前方一致する稼働中SKUもありません"
            continue
        if info.archived:
            hints = await suggest(session, a.code)
            a.verdict = "アーカイブ済"
            a.detail = f"候補: {', '.join(hints)}" if hints else "後継SKUが見つかりません"
            continue
        if info.unmanaged:
            a.verdict, a.detail = "在庫管理対象外", "売上は計上、在庫は動きません"
            continue
        if info.is_bundle or info.has_components:
            a.verdict, a.detail = "共有在庫の親", "売上は親、在庫は構成品から減ります"
            continue

        others = [x for x in shared[a.code] if x is not a]
        if others:
            a.verdict = "OK"
            a.detail = f"同じコードの行が他に{len(others)}件"
            continue
        a.verdict, a.detail = "OK", ""

    return Report(answers=answers)


_ORDER = (
    "該当なし",
    "アーカイブ済",
    "共有在庫の親",
    "在庫管理対象外",
    "未記入",
    "既にマッピング済",
    "対象外",
    "OK",
)


def print_report(report: Report) -> int:
    groups = report.by_verdict()
    total = len(report.answers)
    print(f"\n  === 記入済みワークシートの検証 ===  {total}件")

    problems = 0
    for verdict in _ORDER:
        rows = groups.get(verdict, [])
        if not rows:
            continue
        amount = sum(r.amount for r in rows)
        flag = verdict in ("該当なし", "アーカイブ済", "未記入")
        if flag:
            problems += len(rows)
        mark = "★ " if flag else "  "
        print(f"\n  {mark}{verdict}  {len(rows)}件 / {amount:,} 円")
        if verdict == "OK":
            continue
        for r in rows[:20]:
            print(f"      {r.channel:<8}{r.channel_sku:<16}-> {r.code:<20}{r.detail}")
        if len(rows) > 20:
            print(f"      ... ほか {len(rows) - 20}件")

    print("\n  --- まとめ ---")
    ok = len(groups.get("OK", []))
    print(f"    そのまま取り込める        {ok:>4}件")
    print(f"    確認が必要                {problems:>4}件")
    if problems:
        print("\n  ★ 確認が必要な行があります。取込前にクライアントへ照会してください。")
        print("    アーカイブ済は、バリアント移行前の旧コードを指しています。")
        print("    色・サイズ単位の後継SKUを候補として表示しています。")
    else:
        print("\n  すべて取り込めます")
    return problems


def write_import_csv(report: Report, path: Path) -> int:
    """Only the rows that need no judgement. Anything flagged stays out."""
    rows = [[a.code, a.channel, a.channel_sku, ""] for a in report.answers if a.ok]
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["master_sku_code", "channel", "channel_sku", "channel_product_id"]
    path.write_text(UTF8_BOM + csv_body(header, rows), encoding="utf-8")
    return len(rows)


async def run(*, source: Path, out: Path | None) -> int:
    answers = read_worksheet(source)
    async with async_session_factory() as session:
        report = await validate(session, answers)

    problems = print_report(report)
    if out is not None:
        written = write_import_csv(report, out)
        print(f"\n  取込用CSV -> {out}  ({written}件)")

    log.info(
        "worksheet_validation.done",
        rows=len(answers),
        ok=sum(1 for a in answers if a.ok),
        problems=problems,
    )
    return 0 if problems == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Validate the client's filled-in worksheet")
    p.add_argument("--in", dest="source", required=True, type=Path)
    p.add_argument("--out", type=Path, default=None, help="write the importable rows here")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(source=args.source, out=args.out)))


if __name__ == "__main__":
    main()
