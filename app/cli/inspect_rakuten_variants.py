"""Read-only: what variant does each Rakuten 管理番号 actually refer to? (P2-038)

`inspect_rakuten_payload` established that every order line carries
`SkuModelList`. This reads its VALUES, which answers two questions at once:

**The mapping backlog.** The client answered the unmapped worksheet at product
level — `B46` — because that is what a product is called. Our masters are
variant level (`B46goldanklet`), so 173 of 177 answers pointed at masters the
variant cutover archived. If the payload names the variant, the colour and size
can be recovered from data we already hold instead of asking again.

**P2-038.** Pushing stock to 項目選択肢別在庫 needs the SKU-level key. If
`merchantDefinedSkuId` is populated, the spec investigation has its answer and
the push has something to write against.

PRIVACY: a 楽天 order payload contains the purchaser's name, address and phone
number. This reads ONLY the product-identity fields below. Nothing personal is
read, printed or written.

    powershell -File scripts/run_cli.ps1 -Cli inspect_rakuten_variants ^
        -Args "--unmapped-only --worksheet csv_file/phase2/source/unmapped_worksheet_Corrected.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku, Order, OrderItem
from app.services.sku_scope import operational_conditions

log = get_logger(__name__)

_MAX_ROWS = 30

#: `10002goldanklet` -> `goldanklet`; `B66-g-m` -> `g-m`. The shop's Rakuten SKU
#: ids carry the variant as a suffix after the product token, in at least two
#: spellings. Stripping the leading token is what makes the suffix comparable
#: with our own master codes.
_LEADING_TOKEN = re.compile(r"^[A-Za-z]*[0-9]+")


def variant_suffix(merchant_sku_id: str) -> str:
    """The variant part of a merchantDefinedSkuId, or empty if it has none."""
    rest = _LEADING_TOKEN.sub("", merchant_sku_id, count=1)
    return rest.lstrip("-_").strip()


def candidate_master_code(product_code: str, merchant_sku_id: str) -> str:
    """The master SKU code implied by the client's answer plus the payload.

    `B21` + `10002goldanklet` -> `B21goldanklet`.

    The hyphenated spelling (`B66-g-m`) is deliberately NOT expanded. `g` and
    `m` are not the words our codes use, and deciding that `g` means gold is
    precisely the silent wrong mapping this whole exercise exists to prevent.
    """
    suffix = variant_suffix(merchant_sku_id)
    if not suffix or "-" in suffix:
        return ""
    return f"{product_code}{suffix}"


@dataclass
class ManageNumber:
    """One Rakuten 管理番号 and every variant identity seen under it."""

    code: str
    lines: int = 0
    merchant_sku_ids: set[str] = field(default_factory=set)
    variant_ids: set[str] = field(default_factory=set)
    choices: set[str] = field(default_factory=set)

    @property
    def resolved(self) -> bool:
        """Does this code name exactly one variant?

        More than one means the 管理番号 is the PRODUCT and the SKU is chosen
        per line — which is what 項目選択肢別在庫 means, and why a mapping keyed
        on the 管理番号 alone cannot represent it.
        """
        return len(self.merchant_sku_ids) == 1 or len(self.variant_ids) == 1


def _variant_fields(line: dict[str, Any]) -> tuple[str, str, str]:
    sku_models = line.get("SkuModelList") or line.get("skuModelList") or []
    merchant, variant = "", ""
    if isinstance(sku_models, list) and sku_models:
        first = sku_models[0] or {}
        merchant = str(first.get("merchantDefinedSkuId") or "")
        variant = str(first.get("variantId") or "")
    return merchant, variant, str(line.get("selectedChoice") or "").strip()


def collect(payload: Any, only: set[str] | None) -> list[tuple[str, str, str, str]]:
    if not isinstance(payload, dict):
        return []
    out = []
    for package in payload.get("PackageModelList") or []:
        for line in package.get("ItemModelList") or []:
            code = str(line.get("manageNumber") or line.get("itemNumber") or "")
            if not code or (only is not None and code not in only):
                continue
            merchant, variant, choice = _variant_fields(line)
            out.append((code, merchant, variant, choice))
    return out


async def unmapped_codes(session: AsyncSession) -> set[str]:
    rows = await session.execute(
        select(OrderItem.channel_sku)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == "rakuten", OrderItem.master_sku_id.is_(None))
        .distinct()
    )
    return set(rows.scalars().all())


async def live_master_codes(session: AsyncSession) -> set[str]:
    # The retired-master rule lives in sku_scope, not here. Bundles and
    # unmanaged masters stay in: both are legitimate targets for revenue.
    rows = await session.execute(
        select(MasterSku.sku_code).where(*operational_conditions(include_unmanaged=True))
    )
    return set(rows.scalars().all())


async def survey(
    session: AsyncSession, *, only: set[str] | None, limit: int
) -> dict[str, ManageNumber]:
    stmt = (
        select(Order.raw_payload)
        .where(Order.channel == "rakuten", Order.raw_payload.is_not(None))
        .order_by(Order.ordered_at.desc())
        .limit(limit)
    )
    found: dict[str, ManageNumber] = {}
    for (payload,) in (await session.execute(stmt)).all():
        for code, merchant, variant, choice in collect(payload, only):
            entry = found.setdefault(code, ManageNumber(code=code))
            entry.lines += 1
            if merchant:
                entry.merchant_sku_ids.add(merchant)
            if variant:
                entry.variant_ids.add(variant)
            if choice:
                entry.choices.add(choice)
    return found


def read_answers(path: Path) -> dict[str, str]:
    """{manageNumber: the product code the client wrote}."""
    text = path.read_text(encoding="utf-8-sig")
    out: dict[str, str] = {}
    for row in csv.DictReader(text.splitlines()):
        if (row.get("チャネル") or "").strip() != "rakuten":
            continue
        code = (row.get("チャネルの商品コード") or "").strip()
        answer = (row.get("正しい商品コード ご記入ください") or "").strip()
        if code and answer:
            out[code] = answer
    return out


def report(found: dict[str, ManageNumber], *, scope: str) -> None:
    print(f"\n  === 楽天 管理番号ごとのバリアント識別  {scope} ===")
    if not found:
        print("  該当する受注がありません")
        return

    resolved = [m for m in found.values() if m.resolved]
    with_merchant = [m for m in found.values() if m.merchant_sku_ids]
    with_choice = [m for m in found.values() if m.choices]

    print(f"\n  管理番号 {len(found)}件")
    print(f"    merchantDefinedSkuId あり  {len(with_merchant):>4}件")
    print(f"    selectedChoice あり        {len(with_choice):>4}件")
    print(f"    バリアントが一意に定まる    {len(resolved):>4}件")
    print(f"    複数バリアントを含む        {len(found) - len(resolved):>4}件")

    print(f"\n    {'管理番号':<14}{'明細':>5}  {'merchantDefinedSkuId':<26}選択肢")
    for m in sorted(found.values(), key=lambda x: x.lines, reverse=True)[:_MAX_ROWS]:
        merchant = ", ".join(sorted(m.merchant_sku_ids)[:2]) or "-"
        choice = " / ".join(sorted(m.choices)[:2]) or "-"
        mark = "" if m.resolved else "  ★複数"
        print(f"    {m.code:<14}{m.lines:>5}  {merchant[:24]:<26}{choice[:24]}{mark}")
    if len(found) > _MAX_ROWS:
        print(f"    ... ほか {len(found) - _MAX_ROWS}件")


def report_derivation(
    found: dict[str, ManageNumber], answers: dict[str, str], live: set[str]
) -> None:
    print("\n  --- クライアント回答との突合 ---")
    derivable: list[tuple[str, str, str]] = []
    unknown: list[tuple[str, str]] = []
    no_payload: list[str] = []

    for code, entry in found.items():
        product = answers.get(code)
        if not product:
            continue
        if not entry.merchant_sku_ids:
            no_payload.append(code)
            continue
        for merchant in sorted(entry.merchant_sku_ids):
            guess = candidate_master_code(product, merchant)
            if guess and guess in live:
                derivable.append((code, merchant, guess))
            else:
                unknown.append((merchant, guess or "(接尾辞を解釈できません)"))

    print(f"    回答のある管理番号             {len(answers):>4}件")
    print(f"    稼働中マスタまで復元できた組   {len(derivable):>4}組")
    print(f"    接尾辞から特定できない組       {len(unknown):>4}組")
    print(f"    ペイロードに識別子が無い       {len(no_payload):>4}件")

    if derivable:
        print(f"\n    {'管理番号':<14}{'楽天SKU':<26}-> マスタSKU")
        for code, merchant, guess in derivable[:15]:
            print(f"    {code:<14}{merchant[:24]:<26}-> {guess}")
        if len(derivable) > 15:
            print(f"    ... ほか {len(derivable) - 15}組")

    if unknown:
        print("\n    特定できなかった例:")
        for merchant, guess in unknown[:8]:
            print(f"      {merchant:<26} -> {guess}")


async def run(*, unmapped_only: bool, limit: int, worksheet: Path | None) -> int:
    async with async_session_factory() as session:
        only = await unmapped_codes(session) if unmapped_only else None
        found = await survey(session, only=only, limit=limit)
        live = await live_master_codes(session) if worksheet else set()

    report(found, scope="未マッピングのみ" if unmapped_only else "全件")
    if worksheet:
        report_derivation(found, read_answers(worksheet), live)

    log.info(
        "rakuten_variants.done",
        codes=len(found),
        resolved=sum(1 for m in found.values() if m.resolved),
        unmapped_only=unmapped_only,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Read variant identity out of Rakuten payloads")
    p.add_argument("--unmapped-only", action="store_true", help="only 管理番号 with unmapped lines")
    p.add_argument("--limit", type=int, default=3000, help="orders to scan, newest first")
    p.add_argument("--worksheet", type=Path, default=None, help="the client's filled-in CSV")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(unmapped_only=args.unmapped_only, limit=args.limit, worksheet=args.worksheet)
        )
    )


if __name__ == "__main__":
    main()
