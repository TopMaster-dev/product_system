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
2. **The per-SKU mappings we already hold.** Most Rakuten mapping rows are
   variant-level truth filed under the wrong column — 480 of 512 carry a
   SKU管理番号 in `channel_sku` — and the masters they name are already per
   colour and size. This is the richest source, and the one to reach for first.
3. **Sales history**, but as a fallback and with its limits understood: a
   resolved order line only repeats whatever the mapping said when the line was
   ingested, which for Rakuten was page-level. See `assign_masters`.
4. **The variant attributes in the export**, which say which SKU on one page is
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
    header line in the export, not a SKU, and is skipped.

    THE 商品名 LIVES ON THAT HEADER LINE, NOT ON THE SKU ROWS. Measured over the
    15 October export: 商品名 is filled on 419 of 1,530 rows — exactly the
    product rows — and empty on every SKU row. Reading it from the SKU row
    therefore yields nothing, which is how 93 masters came to be created named
    `10079 gold(3.6mm)` instead of `チェーンネックレス … gold(3.6mm)`. The client
    reads these off a count sheet, so the name is not decoration.
    """
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
    name_of: dict[str, str] = {}
    for line, row in enumerate(rows[1:], start=2):
        manage_number, variant_id = cell(row, COL_MANAGE_NUMBER), cell(row, COL_VARIANT_ID)
        if not variant_id:
            # The product's header line: the only place its 商品名 appears.
            product_name = cell(row, COL_ITEM_NAME)
            if manage_number and product_name:
                name_of.setdefault(manage_number, product_name)
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
                name=cell(row, COL_ITEM_NAME) or name_of.get(manage_number, ""),
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
VIA_MAPPING = "既存のSKU単位マッピング"
VIA_DECIDED = "確認済みの判断"
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


def links_from_legacy_mappings(
    skus: list[RakutenSku], by_channel_sku: dict[str, int]
) -> dict[tuple[str, str], int]:
    """(商品管理番号, SKU管理番号) -> master, read out of the mappings we already hold.

    Most Rakuten mappings in this shop are variant-level truth filed under the
    wrong key: measured 2026-10-19, 480 of 512 active rows carry a SKU管理番号 in
    `channel_sku`, where the column is supposed to hold the 商品管理番号. The
    masters they name are already per colour and size (`1467` is "B08 gold"), so
    the information needed to map the catalogue is largely present — it just has
    no page attached to it. The export supplies the page.

    Two kinds of value are refused.

    A value that is ALSO a 商品管理番号 is skipped. Ten such rows exist, and
    reading one of them as a SKU when it was filed as a page would attach a
    master to the wrong variant with nothing to contradict it.

    **A PLACEHOLDER is skipped, and this one is not a nicety.** RMS auto-numbers
    a SKU `r-sku00000001` when the shop has not supplied its own code, so the
    value identifies nothing — and the same `r-sku00000001` appears on 24
    DIFFERENT products in this catalogue. An old mapping filed under it points
    at `B45goldbracelet`, so reading placeholders as SKU keys attached six
    unrelated products to that one master, and scattered one Christmas set's
    nine variants across three categories. Measured 2026-10-19: 89 such links
    over 63 variants in 16 products, every one of them wrong.
    """
    pages = {sku.manage_number for sku in skus}
    links: dict[tuple[str, str], int] = {}
    for sku in skus:
        master_id = by_channel_sku.get(sku.variant_id)
        if master_id is None or sku.is_placeholder or sku.variant_id in pages:
            continue
        links[sku.key] = master_id
    return links


def _assign_from_links(
    variant: PhysicalVariant,
    links: dict[tuple[str, str], int],
    label: str,
    master_by_code: dict[str, tuple[int, str]],
) -> VariantAssignment | None:
    """One variant's answer from one source, or None if the source is silent."""
    masters = {links[sku.key] for sku in variant.skus if sku.key in links}
    if not masters:
        return None
    if len(masters) > 1:
        return VariantAssignment(variant, None, "", label, tuple(sorted(masters)))
    master_id = masters.pop()
    code = next((c for mid, c in master_by_code.values() if mid == master_id), "")
    return VariantAssignment(variant, master_id, code, label)


def assign_masters(
    variants: list[PhysicalVariant],
    *,
    proven: dict[tuple[str, str], int],
    master_by_code: dict[str, tuple[int, str]],
    legacy: dict[tuple[str, str], int] | None = None,
    archived: set[int] | None = None,
) -> list[VariantAssignment]:
    """The existing per-SKU mapping first, then sales history, then SKU codes.

    A variant inherits a master found for ANY of its SKUs, which is what makes
    the pair table pay off: one link on the men's page maps the women's page too.

    THE ORDER IS THE OPPOSITE OF WHAT IT LOOKS LIKE IT SHOULD BE, and that is
    measured rather than assumed. "Sales history" sounds like the harder fact,
    but `links_from_orders` reads `order_items.master_sku_id`, which the resolver
    wrote using whatever mapping existed at the time — so it is a CACHE of past
    mapping decisions at whatever granularity those decisions had, not
    independent evidence. Rakuten ingest keys on `channel_sku = manageNumber`,
    i.e. the PAGE, so every size and colour of a product was resolved to one
    page-level master and now reports that master as if it were a per-variant
    fact.

    Measured against production 2026-10-19, over 587 physical variants:
    the two sources agreed **0** times and disagreed **183** times, and every
    disagreement had the same shape — 受注実績 naming a page-coded master
    (`0010c`, `0020c`, `005c`) where the mapping named a variant-level one
    (`N21gold`, `R09goldus7`, `N11gold`). In 129 of the 183 the 受注実績 master
    demonstrably covered several variants at once.

    So the per-SKU mapping wins: it is variant-level by construction. Sales
    history still answers for the 24 variants nothing else reaches, and where it
    hands back a page-level master the double-claim check downstream catches it.
    Matching on codes is last because it is inference from spelling.
    """
    sources = ((legacy or {}, VIA_MAPPING), (proven, VIA_ORDERS))
    by_codeish = codeish_index(master_by_code, archived)
    out: list[VariantAssignment] = []
    for variant in variants:
        answer = None
        for links, label in sources:
            answer = _assign_from_links(variant, links, label, master_by_code)
            if answer is not None:
                break
        if answer is not None:
            out.append(answer)
            continue

        found: tuple[int, str] | None = None
        via = ""
        for sku in variant.skus:
            merchant = _codeish(sku.merchant_sku_id)
            if merchant and merchant in by_codeish:
                found, via = by_codeish[merchant], VIA_MERCHANT
                break
            variant_code = "" if sku.is_placeholder else _codeish(sku.variant_id)
            if variant_code and variant_code in by_codeish:
                found, via = by_codeish[variant_code], VIA_VARIANT
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


def codeish_index(
    master_by_code: dict[str, tuple[int, str]], archived: set[int] | None = None
) -> dict[str, tuple[int, str]]:
    """Master codes indexed without case or punctuation.

    `p07-gold` and `P07gold` are one code written two ways, and comparing them
    literally is how `10060cc` came within one `--apply` of creating duplicates
    of `P07gold` and `P07silver` — products that already existed, were
    stock-managed, and were named by the Rakuten SKU itself.

    Codes that collide under this reading are dropped rather than picked
    between: if two real masters reduce to one token, neither identifies a
    product. `codeish_collisions` reports them.

    Archived masters are left out. A retired product should not take a live
    Rakuten SKU, and measured 2026-10-19 nothing depends on it doing so: zero
    active Rakuten mappings point at an archived master. It also resolves the
    one real collision in the catalogue — `N79` (archived, no stock) against
    `N79-` (live, 281 units), which is one product entered twice. Without this
    both were dropped and the live one lost its match.
    """
    retired = archived or set()
    groups: dict[str, set[tuple[int, str]]] = {}
    for master_id, code in master_by_code.values():
        key = _codeish(code)
        if key and master_id not in retired:
            groups.setdefault(key, set()).add((master_id, code))
    return {key: next(iter(found)) for key, found in groups.items() if len(found) == 1}


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


# --- 人が判断した矛盾の解消 ------------------------------------------------------------

#: Columns of the reviewed decision file.
DECISION_COL_GROUP = "商品管理番号"
DECISION_COL_LABEL = "選択肢"
DECISION_COL_CODE = "採用する商品コード"


def read_conflict_decisions(path: Path) -> tuple[dict[tuple[str, str], str], list[str]]:
    """(商品管理番号, 選択肢) -> the master code a person chose.

    A conflict means one physical variant's SKUs point at different products,
    which for this shop is a reused SKU管理番号 spanning unrelated things — an
    anklet and a ring under one number. No rule settles that; somebody has to
    look at the two products. This file is where those judgements live, as data
    that can be re-read, corrected and argued with rather than as code.

    Missing file is not an error: it means nothing has been decided yet.
    """
    if not path.exists():
        return {}, []
    text = _decode(path)
    if text is None:
        return {}, ["判断ファイルの文字コードを判別できませんでした"]
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return {}, []
    header = [cell.strip().lstrip("﻿") for cell in rows[0]]
    required = (DECISION_COL_GROUP, DECISION_COL_LABEL, DECISION_COL_CODE)
    if any(column not in header for column in required):
        return {}, [f"判断ファイルに必要な列がありません: {', '.join(required)}"]
    index = {name: header.index(name) for name in required}

    decisions: dict[tuple[str, str], str] = {}
    problems: list[str] = []
    for line, row in enumerate(rows[1:], start=2):
        if len(row) <= max(index.values()):
            continue
        group = row[index[DECISION_COL_GROUP]].strip()
        label = row[index[DECISION_COL_LABEL]].strip()
        code = row[index[DECISION_COL_CODE]].strip()
        if not (group and label and code):
            continue
        key = (group, label)
        if key in decisions and decisions[key] != code:
            problems.append(f"{line}行目: {group} / {label} の判断が重複しています")
            continue
        decisions[key] = code
    return decisions, problems


def apply_decisions(
    assignments: list[VariantAssignment],
    *,
    decisions: dict[tuple[str, str], str],
    master_by_code: dict[str, tuple[int, str]],
) -> tuple[list[VariantAssignment], list[str]]:
    """Settle conflicts a person has already judged, and nothing else.

    Two restrictions make a stale or mistyped file harmless rather than
    dangerous:

    * only a CONFLICTED variant is touched, so a decision cannot overrule a
      mapping the data agrees about;
    * the chosen code must be one of the candidates the data actually offered.
      A typo, or a decision left behind after the catalogue moved on, therefore
      cannot invent a mapping — it is reported and the conflict stands.
    """
    settled: list[VariantAssignment] = []
    problems: list[str] = []
    for assignment in assignments:
        key = (assignment.variant.group, assignment.variant.label)
        code = decisions.get(key)
        if code is None or not assignment.conflicting:
            settled.append(assignment)
            continue
        found = master_by_code.get(code.strip().lower())
        if found is None:
            problems.append(f"{key[0]} / {key[1]}: 商品コード {code} が見つかりません")
            settled.append(assignment)
            continue
        if found[0] not in assignment.conflicting:
            problems.append(
                f"{key[0]} / {key[1]}: {code} は候補ではありません "
                f"(候補: {', '.join(str(m) for m in assignment.conflicting)})"
            )
            settled.append(assignment)
            continue
        settled.append(VariantAssignment(assignment.variant, found[0], found[1], VIA_DECIDED))
    unused = sorted(
        key
        for key in decisions
        if not any((a.variant.group, a.variant.label) == key and a.conflicting for a in assignments)
    )
    problems.extend(f"{group} / {label}: 対応する矛盾がありません" for group, label in unused)
    return settled, problems


#: Columns of the reviewed exclusion file.
EXCLUDE_COL_PAGE = "商品管理番号"


def read_excluded_pages(path: Path) -> tuple[set[str], list[str]]:
    """商品管理番号 that must not become products of ours.

    Not everything a shop lists is stock. `gift-set` is a gift box: it carries a
    quantity, so every automated route treats it as merchandise, and this
    project already has a -12,509 discrepancy from an item of exactly that shape
    being stock-managed when it should not have been. No rule distinguishes a
    gift box from a bracelet — the quantity, the page and the SKU all look
    identical — so the judgement is recorded here instead of inferred.

    Missing file means nothing is excluded.
    """
    if not path.exists():
        return set(), []
    text = _decode(path)
    if text is None:
        return set(), ["除外ファイルの文字コードを判別できませんでした"]
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return set(), []
    header = [cell.strip().lstrip("﻿") for cell in rows[0]]
    if EXCLUDE_COL_PAGE not in header:
        return set(), [f"除外ファイルに {EXCLUDE_COL_PAGE} の列がありません"]
    column = header.index(EXCLUDE_COL_PAGE)
    pages = {row[column].strip() for row in rows[1:] if len(row) > column and row[column].strip()}
    return pages, []
