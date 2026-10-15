"""The Rakuten catalogue as physical products, not as pages.

The shop lists most products TWICE — a men's page and a women's page, coded `X`
and `Xc`. The client gave us the pairing in September (198 pairs, reconciled
against the 15 October export on 2026-10-15: still exact), and asked for the two
pages to share one stock figure. So a Rakuten SKU is not a thing to hold stock
for; a PHYSICAL VARIANT is, and it usually has two Rakuten SKUs pointing at it.

    物理バリアント  = (ページの組, 色・サイズ)
                    -> 073/1435 (メンズ gold 19cm) と 073c/931 (レディース gold 19cm)

Three sources, in descending order of trust:

1. **The client's pair table.** Authoritative, and it must be — a plain
   "X and Xc" rule looks right and is not: it pairs `10060c` with `10060cc`,
   which the client's own 未ペア sheet records as a broken duplicate.
2. **Sales history.** A resolved Rakuten order line names its master, so where
   a sale happened the mapping is a fact. The codes cannot be matched any other
   way: Rakuten numbers its products (`00037c`, `0020`) while our masters came
   from Shopify with letter codes, and there is no JAN on either side.
3. **The variant attributes in the export**, which say which SKU on one page is
   the same thing as which SKU on the other.

Nothing here writes. Building the mapping is a separate, reviewed step.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order, OrderItem

RAKUTEN = "rakuten"

#: RMS writes CP932. The rest are tried so a file re-saved from Excel still loads.
ENCODINGS = ("cp932", "utf-8-sig", "utf-8")

#: Matched by name, not position: the export carries 575 columns.
COL_MANAGE_NUMBER = "商品管理番号（商品URL）"  # noqa: RUF001 (RMS uses full-width parens)
COL_VARIANT_ID = "SKU管理番号"
COL_MERCHANT_SKU = "システム連携用SKU番号"
COL_QUANTITY = "在庫数"
COL_ITEM_NAME = "商品名"
COL_CHOICE_1 = "バリエーション項目選択肢1"
COL_CHOICE_2 = "バリエーション項目選択肢2"

#: Columns of the client's pair table.
PAIR_COL_MENS = "商品管理番号(メンズ/無印)"
PAIR_COL_LADIES = "商品管理番号(レディース/c)"


@dataclass(frozen=True, slots=True)
class RakutenSku:
    manage_number: str
    variant_id: str
    merchant_sku_id: str
    quantity: int
    name: str
    choices: tuple[str, ...] = ()

    @property
    def is_placeholder(self) -> bool:
        """RMS auto-numbered the SKU and nobody renamed it. Harmless for the
        push — the pair is still unique — but unreadable to a person."""
        return self.variant_id.startswith("r-sku")

    @property
    def key(self) -> tuple[str, str]:
        return (self.manage_number, self.variant_id)


def _decode(path: Path) -> str | None:
    for encoding in ENCODINGS:
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return None


def read_catalogue(path: Path) -> tuple[list[RakutenSku], list[str]]:
    """(SKU rows, problems). A row with no SKU管理番号 is the product's own
    header line in the export, not a SKU, and is skipped."""
    text = _decode(path)
    if text is None:
        return [], [f"文字コードを判別できませんでした ({', '.join(ENCODINGS)} を試行)"]

    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return [], ["ファイルが空です"]
    header = {name.strip(): i for i, name in enumerate(rows[0])}
    missing = [c for c in (COL_MANAGE_NUMBER, COL_VARIANT_ID, COL_QUANTITY) if c not in header]
    if missing:
        return [], [f"必須列がありません: {', '.join(missing)}"]

    def cell(row: list[str], column: str) -> str:
        index = header.get(column, -1)
        return row[index].strip() if 0 <= index < len(row) else ""

    skus: list[RakutenSku] = []
    problems: list[str] = []
    seen: set[tuple[str, str]] = set()
    for line, row in enumerate(rows[1:], start=2):
        manage_number, variant_id = cell(row, COL_MANAGE_NUMBER), cell(row, COL_VARIANT_ID)
        if not variant_id:
            continue
        if not manage_number:
            problems.append(f"{line}行目: SKU管理番号はあるが商品管理番号が空です")
            continue
        if (manage_number, variant_id) in seen:
            problems.append(
                f"{line}行目: 同じ組み合わせが重複しています {manage_number}/{variant_id}"
            )
            continue
        seen.add((manage_number, variant_id))
        raw = cell(row, COL_QUANTITY)
        choices = tuple(c for c in (cell(row, COL_CHOICE_1), cell(row, COL_CHOICE_2)) if c)
        skus.append(
            RakutenSku(
                manage_number=manage_number,
                variant_id=variant_id,
                merchant_sku_id=cell(row, COL_MERCHANT_SKU),
                quantity=int(raw) if raw.lstrip("-").isdigit() else 0,
                name=cell(row, COL_ITEM_NAME),
                choices=choices,
            )
        )
    return skus, problems


# --- the client's pair table ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PagePair:
    mens: str
    ladies: str


def read_pair_table(path: Path) -> tuple[list[PagePair], list[str]]:
    text = _decode(path)
    if text is None:
        return [], ["ペア対応表の文字コードを判別できませんでした"]
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return [], ["ペア対応表が空です"]
    header = {name.strip(): i for i, name in enumerate(rows[0])}
    if PAIR_COL_MENS not in header or PAIR_COL_LADIES not in header:
        return [], [f"必須列がありません: {PAIR_COL_MENS} / {PAIR_COL_LADIES}"]
    mens_at, ladies_at = header[PAIR_COL_MENS], header[PAIR_COL_LADIES]

    pairs: list[PagePair] = []
    problems: list[str] = []
    seen: set[str] = set()
    for line, row in enumerate(rows[1:], start=2):
        if max(mens_at, ladies_at) >= len(row):
            continue
        mens, ladies = row[mens_at].strip(), row[ladies_at].strip()
        if not mens or not ladies:
            continue
        clash = {mens, ladies} & seen
        if clash:
            problems.append(f"{line}行目: 既に別の組で使われています {', '.join(sorted(clash))}")
            continue
        seen |= {mens, ladies}
        pairs.append(PagePair(mens=mens, ladies=ladies))
    return pairs, problems


# --- physical variants ---------------------------------------------------------------

#: One page writes a size as "US5 (9-10go)" and its twin as "USA5(9-10go no aida)"
#: in full-width characters. Pairing fails without normalisation, and a failed
#: pairing silently splits one product in two — so this is part of the mapping,
#: not cosmetics. The class covers ASCII and full-width brackets alike.
_PARENTHETICAL = re.compile("[(（].*?[)）]")  # noqa: RUF001 (both bracket widths are intended)


def normalise_choice(value: str, *, strip_parenthetical: bool = True) -> str:
    """Reduce one 選択肢 to a comparable token.

    `strip_parenthetical` is the dangerous switch. The shop uses brackets for two
    unrelated jobs: restating a US ring size in Japanese 号 — where the two pages
    word it differently and stripping is the only way they meet — and carrying a
    real specification. Measured over the 15 October export, 11 of 25 distinct
    bracket contents are specifications (`3.6mm`, `手用`, `片耳`, `us5`, `19cm`),
    so stripping unconditionally merges products that differ. `group_variants`
    picks the setting per product rather than guessing from the wording.
    """
    text = unicodedata.normalize("NFKC", value).lower()
    if strip_parenthetical:
        text = _PARENTHETICAL.sub("", text)
    text = text.replace(" ", "").replace("&", "").replace("　", "")
    text = text.replace("usa", "us")
    return text.rstrip("号").strip("-_/")


def _identity(sku: RakutenSku, *, strip_parenthetical: bool) -> tuple[str, ...]:
    return tuple(
        sorted(
            normalise_choice(choice, strip_parenthetical=strip_parenthetical)
            for choice in sku.choices
            if choice.strip()
        )
    )


def _collides_within_a_page(skus: list[RakutenSku], *, strip_parenthetical: bool) -> bool:
    """Whether this reading makes one Rakuten page list the same variant twice.

    A page never sells one physical variant from two SKUs — the shop could not
    tell which to decrement. So a collision inside a single page is proof that
    the reading threw away something that distinguishes them, and that is the
    over-matching direction: it would write one quantity onto several live SKUs.
    Across pages a collision is the opposite — it is the pairing we want.
    """
    seen: dict[str, set[tuple[str, ...]]] = {}
    for sku in skus:
        page = seen.setdefault(sku.manage_number, set())
        identity = _identity(sku, strip_parenthetical=strip_parenthetical)
        if identity in page:
            return True
        page.add(identity)
    return False


@dataclass(frozen=True, slots=True)
class PhysicalVariant:
    """One real thing on a shelf, and the Rakuten SKUs that sell it."""

    group: str
    choices: tuple[str, ...]
    skus: tuple[RakutenSku, ...]
    #: True where the brackets in this product's 選択肢 carry a specification, so
    #: they were kept. See `normalise_choice`.
    kept_brackets: bool = False

    @property
    def label(self) -> str:
        return " / ".join(self.choices) if self.choices else "(選択肢なし)"

    @property
    def pages(self) -> tuple[str, ...]:
        return tuple(sorted({s.manage_number for s in self.skus}))

    @property
    def quantities(self) -> tuple[int, ...]:
        return tuple(s.quantity for s in self.skus)

    @property
    def duplicated_pages(self) -> tuple[str, ...]:
        """Pages contributing more than one SKU to this variant.

        Expected to be empty. Anything here is either a duplicate SKU on a live
        page or a variant axis the export does not expose, and in both cases the
        quantity to push is undecidable — so it must be read, never averaged.
        """
        counts = Counter(sku.manage_number for sku in self.skus)
        return tuple(sorted(page for page, n in counts.items() if n > 1))


def group_variants(skus: list[RakutenSku], pairs: list[PagePair]) -> list[PhysicalVariant]:
    """Fold the two pages of a product together, then split by colour and size.

    Grouped on the SORTED set of normalised choices, so a page that lists colour
    first and size second still meets its twin that does the opposite.

    Brackets are decided per product, not globally: stripping them is tried
    first, and kept only while no single page ends up listing one variant twice.
    Measured over the 15 October export, 219 of 221 products strip cleanly and 2
    (`10079`, `202512a`) do not — those two hold a chain width and a ring size in
    brackets, and stripping folded their 17 SKUs into 5 variants instead of 13.
    """
    group_of = {pair.ladies: pair.mens for pair in pairs}
    group_of.update({pair.mens: pair.mens for pair in pairs})

    members_of: dict[str, list[RakutenSku]] = {}
    for sku in skus:
        members_of.setdefault(group_of.get(sku.manage_number, sku.manage_number), []).append(sku)

    variants: list[PhysicalVariant] = []
    for group, members in members_of.items():
        strip = not _collides_within_a_page(members, strip_parenthetical=True)
        buckets: dict[tuple[str, ...], list[RakutenSku]] = {}
        for sku in members:
            buckets.setdefault(_identity(sku, strip_parenthetical=strip), []).append(sku)
        variants.extend(
            PhysicalVariant(
                group=group, choices=choices, skus=tuple(bucket), kept_brackets=not strip
            )
            for choices, bucket in buckets.items()
        )
    return sorted(variants, key=lambda v: (v.group, v.choices))


# --- what the orders already prove ----------------------------------------------------

#: Rakuten orders read per round trip. Payloads are large, so they are streamed.
ORDER_PAGE = 300


async def links_from_orders(session: AsyncSession) -> dict[tuple[str, str], int]:
    """(商品管理番号, SKU管理番号) -> master, proven by a real sale.

    Reads order payloads for `itemDetailId`, `manageNumber` and `variantId`
    only. A purchaser's name, address and phone are never touched.
    """
    resolved = {
        (order_id, line_id): master_id
        for order_id, line_id, master_id in (
            await session.execute(
                select(OrderItem.order_id, OrderItem.line_id, OrderItem.master_sku_id)
                .join(Order, Order.id == OrderItem.order_id)
                .where(Order.channel == RAKUTEN, OrderItem.master_sku_id.is_not(None))
            )
        ).all()
    }
    if not resolved:
        return {}

    links: dict[tuple[str, str], Counter[int]] = {}
    offset = 0
    while True:
        rows = (
            await session.execute(
                select(Order.id, Order.raw_payload)
                .where(Order.channel == RAKUTEN, Order.raw_payload.is_not(None))
                .order_by(Order.id)
                .offset(offset)
                .limit(ORDER_PAGE)
            )
        ).all()
        if not rows:
            break
        offset += len(rows)
        for order_id, payload in rows:
            if not isinstance(payload, dict):
                continue
            for pkg in payload.get("PackageModelList") or []:
                for line in pkg.get("ItemModelList") or []:
                    line_id = str(line.get("itemDetailId") or line.get("itemNumber") or "")
                    master_id = resolved.get((order_id, line_id))
                    if master_id is None:
                        continue
                    manage_number = str(line.get("manageNumber") or "").strip()
                    models = line.get("SkuModelList") or line.get("skuModelList") or []
                    first = models[0] if models and isinstance(models[0], dict) else {}
                    variant_id = str(first.get("variantId") or "").strip()
                    if manage_number and variant_id:
                        links.setdefault((manage_number, variant_id), Counter())[master_id] += 1

    # A SKU that resolved to two masters over time means the mapping was
    # corrected at some point; the most recent intent is the majority one.
    return {pair: counter.most_common(1)[0][0] for pair, counter in links.items()}


# --- assigning a master to each physical variant ---------------------------------------


VIA_ORDERS = "受注実績"
VIA_MERCHANT = "システム連携用SKU番号"
VIA_VARIANT = "SKU管理番号"


@dataclass(frozen=True, slots=True)
class VariantAssignment:
    """One physical variant and the master we believe sells it."""

    variant: PhysicalVariant
    master_id: int | None
    master_code: str
    via: str
    #: More than one master proven for the SAME variant. Never guessed past —
    #: two pages of one product resolving to different masters means one of the
    #: existing mappings is wrong, and picking either would hide that.
    conflicting: tuple[int, ...] = ()

    @property
    def resolved(self) -> bool:
        return self.master_id is not None and not self.conflicting


def assign_masters(
    variants: list[PhysicalVariant],
    *,
    proven: dict[tuple[str, str], int],
    master_by_code: dict[str, tuple[int, str]],
) -> list[VariantAssignment]:
    """Sales history first, then the shop's own SKU codes.

    A variant inherits a master proven for ANY of its SKUs, which is what makes
    the pair table pay off: one sale on the men's page maps the women's page too.
    """
    out: list[VariantAssignment] = []
    for variant in variants:
        masters = {proven[sku.key] for sku in variant.skus if sku.key in proven}
        if len(masters) > 1:
            out.append(VariantAssignment(variant, None, "", VIA_ORDERS, tuple(sorted(masters))))
            continue
        if masters:
            master_id = masters.pop()
            code = next(
                (c for mid, c in master_by_code.values() if mid == master_id),
                "",
            )
            out.append(VariantAssignment(variant, master_id, code, VIA_ORDERS))
            continue

        found: tuple[int, str] | None = None
        via = ""
        for sku in variant.skus:
            merchant = sku.merchant_sku_id.strip().lower()
            if merchant and merchant in master_by_code:
                found, via = master_by_code[merchant], VIA_MERCHANT
                break
            if sku.variant_id.strip().lower() in master_by_code:
                found, via = master_by_code[sku.variant_id.strip().lower()], VIA_VARIANT
                break
        if found is not None:
            out.append(VariantAssignment(variant, found[0], found[1], via))
        else:
            out.append(VariantAssignment(variant, None, "", ""))
    return out


def masters_claimed_twice(
    assignments: list[VariantAssignment],
) -> dict[int, list[VariantAssignment]]:
    """Masters that two different physical variants both claim.

    Distinct variants are distinct things on a shelf, so sharing a master means
    one quantity would be written to both — the defect that made the first
    mapping attempt unsafe. Legitimate page sharing is already folded inside a
    variant and never appears here.
    """
    claims: dict[int, list[VariantAssignment]] = {}
    for assignment in assignments:
        if assignment.resolved and assignment.master_id is not None:
            claims.setdefault(assignment.master_id, []).append(assignment)
    return {mid: group for mid, group in claims.items() if len(group) > 1}


# --- splitting a master that two variants claim ---------------------------------------

#: Codes are compared on alphanumerics only, so `B66-gold-M` and `B66goldM` meet.
_NON_CODE = re.compile(r"[^0-9a-z]+")


def _codeish(value: str) -> str:
    return _NON_CODE.sub("", unicodedata.normalize("NFKC", value).lower())


def _consumes(remainder: str, tokens: tuple[str, ...]) -> bool:
    """Whether `remainder` is exactly this variant's attributes and nothing else.

    Longest token first, and the leftover must be empty. Both halves matter:
    a loose "contains" test matches `s` inside `silver`, so `B66silverM` would
    answer for the silver/S variant as readily as for silver/M.
    """
    for token in sorted(tokens, key=len, reverse=True):
        if token not in remainder:
            return False
        remainder = remainder.replace(token, "", 1)
    return remainder == ""


@dataclass(frozen=True, slots=True)
class Split:
    """One variant's way out of a master that several variants claim."""

    variant: PhysicalVariant
    claimed_code: str
    #: Existing masters whose code is the claimed one plus exactly this variant's
    #: attributes. One is an answer; none means the master has to be created;
    #: several means the codes cannot tell the variants apart.
    candidates: tuple[str, ...]

    @property
    def verdict(self) -> str:
        if len(self.candidates) == 1:
            return "付け替え可能"
        return "商品の新規作成が必要" if not self.candidates else "候補が複数 — 要確認"


def codeish_collisions(master_codes: list[str]) -> dict[str, list[str]]:
    """Master codes that reduce to the same comparable form.

    `_codeish` drops case and punctuation, so `B43-gold-M` and `B43goldM` become
    one token. Where two real masters collide, the code no longer identifies a
    master, and the quantity a re-point sends to a live page would depend on
    which row the database happened to return — so the collision has to be read
    by a person, not resolved by ordering.
    """
    groups: dict[str, list[str]] = {}
    for code in master_codes:
        key = _codeish(code)
        if key:
            groups.setdefault(key, []).append(code)
    return {key: sorted(set(codes)) for key, codes in groups.items() if len(set(codes)) > 1}


def split_candidates(
    assignments: list[VariantAssignment], *, master_codes: list[str]
) -> list[Split]:
    """For every master two variants claim, look for per-variant masters already
    in the catalogue.

    The claims come from sales history, which proves a Rakuten SKU sold under
    some master but not that the master is variant-level. Where the Shopify
    import already created one master per colour and size, the fix is to point
    each Rakuten variant at its own — no new products, and the quantity lands in
    one place. Where it did not, the master has to be created, and this says so
    rather than picking a sibling at random.

    A master whose code collides with another under `_codeish` is never offered,
    because the offer would carry a confidence the codes do not support.
    """
    colliding = set(codeish_collisions(master_codes))
    by_code: dict[str, str] = {}
    for code in master_codes:
        key = _codeish(code)
        if key and key not in colliding:
            by_code[key] = code
    splits: list[Split] = []
    for group in masters_claimed_twice(assignments).values():
        claimed = group[0].master_code or ""
        base = _codeish(claimed)
        if not base:
            continue
        siblings = {
            code: low[len(base) :]
            for low, code in by_code.items()
            if low.startswith(base) and low != base
        }
        for assignment in group:
            tokens = tuple(token for token in map(_codeish, assignment.variant.choices) if token)
            matches = (
                tuple(sorted(code for code, rest in siblings.items() if _consumes(rest, tokens)))
                if tokens
                else ()
            )
            splits.append(Split(assignment.variant, claimed, matches))
    return splits
