"""Generate the fill-in sheets the client is asked to complete.

Every sheet is written in the SAME column vocabulary its importer accepts, so
what comes back uploads without anyone editing headers. That is the point: a
worksheet the client fills in correctly and we then cannot read wastes their
afternoon, and this project has already had a returned file rejected by its own
importer over an Excel title row.

  sku_categories_assign.csv   every operational SKU with a SUGGESTED category
                              and the reason for it. Round-trips into
                              `/admin/categories/upload`.
  categories.csv              the registered taxonomy, so the codes above can be
                              looked up while reviewing.

There is deliberately NO Rakuten placeholder sheet. docs/27 §2-2 withdrew that
request on 2026-09-03: `r-sku00000001` repeats under 24 商品管理番号 because RMS
auto-numbers per product page, so the pair (商品管理番号, SKU管理番号) is unique
and a placeholder is a perfectly valid API target. Making the mapping key
composite is OUR work item (P2-039). Asking the client to recreate those SKUs
would reset the sales history on the products carrying them — Rakuten SKU
numbers cannot be changed after creation.

UTF-8 with a BOM, via `csv_export`. Excel decides a CSV's encoding by the BOM
alone; without it a Japanese-locale machine guesses CP932 and an English-locale
one guesses Windows-1252, and 大分類 comes back mojibake either way.

Read-only against the database. Writes files to --out-dir.

    powershell -File scripts/run_cli.ps1 -Cli export_client_worksheets
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from sqlalchemy import select

from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku, ProductCategory
from app.services.categories import load_overview
from app.services.sku_scope import operational_conditions

log = get_logger(__name__)

DEFAULT_OUT = Path("csv_file/phase2/client")

#: SKU code prefix -> category code. The catalogue's own naming convention:
#: N108gold is a necklace, R34silverus5 a ring. Covers most of the list.
_PREFIX_CATEGORY = {
    "N": "NECKLACE",
    "B": "BRACELET",
    "R": "RING",
    "P": "PIERCE",
    "K": "KEYRING",
}

#: Anklets share the B prefix with bracelets — the same chain sold two ways,
#: which is why they also share stock. Only the name separates them, so this is
#: checked BEFORE the prefix.
_ANKLET_WORDS = ("anklet", "アンクレット")

#: For legacy numeric codes (0010c, 009c) whose prefix says nothing. Ordered:
#: the first match wins, so more specific words come first.
_NAME_CATEGORY = (
    ("ネックレス", "NECKLACE"),
    ("necklace", "NECKLACE"),
    ("ブレスレット", "BRACELET"),
    ("bracelet", "BRACELET"),
    ("bangle", "BRACELET"),
    ("バングル", "BRACELET"),
    ("ピアス", "PIERCE"),
    ("pierce", "PIERCE"),
    ("イヤーカフ", "PIERCE"),
    ("キーリング", "KEYRING"),
    ("リング", "RING"),
    ("ring", "RING"),
)


#: The product token as it appears inside a name: "B12 gold", "N46 silver".
_TOKEN_RE = re.compile(r"\b([NBRPK])\d{1,3}\b", re.IGNORECASE)


def suggest_category(sku_code: str, name: str) -> tuple[str, str]:
    """(category_code, why). A blank code means "we could not tell".

    Suggestions are written into the sheet rather than left blank because 714
    empty cells do not get filled in by hand — the sheet comes back untouched
    and the feature ships unverified. The reason is written alongside so the
    client can check the doubtful rows rather than the whole file, and so a
    wrong guess arrives visibly as a guess instead of as data.
    """
    blob = f"{sku_code} {name}".lower()
    if any(word in blob for word in _ANKLET_WORDS):
        return "ANKLET", "商品名"

    head = sku_code[:1].upper()
    if head in _PREFIX_CATEGORY:
        return _PREFIX_CATEGORY[head], "SKUコード"

    lowered = name.lower()
    for word, code in _NAME_CATEGORY:
        if word.lower() in lowered:
            return code, "商品名"

    # Legacy numeric codes often carry the real product token in the NAME
    # instead ("1079" is named "B12 gold"). Same convention, one column over.
    token = _TOKEN_RE.search(name)
    if token:
        head = token.group(1).upper()
        if head in _PREFIX_CATEGORY:
            return _PREFIX_CATEGORY[head], "商品名のコード"
    return "", "要確認"


def _write(path: Path, body: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UTF8_BOM + body, encoding="utf-8")
    return body.count("\n") - 1  # data rows, excluding the header


async def run(*, out_dir: Path = DEFAULT_OUT) -> int:
    async with async_session_factory() as session:
        sku_rows = await session.execute(
            select(MasterSku.sku_code, MasterSku.name, ProductCategory.code)
            .outerjoin(ProductCategory, ProductCategory.id == MasterSku.category_id)
            .where(*operational_conditions())
            .order_by(MasterSku.sku_code)
        )

        # Header uses SKU_CATEGORY_CSV's canonical names. The importer accepts
        # the Japanese aliases too, but the canonical form still matches after a
        # round trip through Excel. `推定根拠` is an extra column the importer
        # ignores, so the client can leave it in place when re-uploading.
        assign: list[list[object]] = []
        needs_check = 0
        for code, name, existing in sku_rows.all():
            if existing:
                # Already assigned; carried through so a partially completed
                # sheet can be re-issued without losing prior work.
                assign.append([code, name, existing, "設定済み"])
                continue
            guess, why = suggest_category(code, name)
            if not guess:
                needs_check += 1
            assign.append([code, name, guess, why])

        assign_count = _write(
            out_dir / "sku_categories_assign.csv",
            csv_body(["sku_code", "商品名", "category_code", "推定根拠"], assign),
        )

        overview = await load_overview(session)
        taxonomy: list[list[object]] = []
        for root in overview.roots:
            taxonomy.append([root.category.code, root.category.name, "", root.own_sku_count])
            for child in root.children:
                taxonomy.append(
                    [
                        child.category.code,
                        root.category.name,
                        child.category.name,
                        child.own_sku_count,
                    ]
                )
        tax_count = _write(
            out_dir / "categories.csv",
            csv_body(["category_code", "大分類", "中分類", "現在の割当SKU数"], taxonomy),
        )

    print(f"\n--- クライアント記入用シート を {out_dir} に出力しました ---\n")
    print(f"  sku_categories_assign.csv   {assign_count:>5}件  カテゴリ割り当て")
    print(f"    うち 要確認 {needs_check}件。残りは推定値を記入済みです")
    print(f"  categories.csv              {tax_count:>5}件  登録済みカテゴリ一覧")

    log.info(
        "client_worksheets.done",
        assign=assign_count,
        needs_check=needs_check,
        categories=tax_count,
        out_dir=str(out_dir),
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Generate the client fill-in worksheets")
    p.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(out_dir=Path(args.out_dir))))


if __name__ == "__main__":
    main()
