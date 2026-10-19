"""楽天 在庫API 2.0 の在庫更新 — リクエストの組み立てと応答の読み取り.

Pure: no HTTP, no session, no clock. Everything that depends on Rakuten's wire
contract lives here so it can be read against the spec in one sitting and tested
without a network.

THE SPEC (client's RMS 仕様書, received 2026-10-19 — not inferred)

    POST /es/2.0/inventories/bulk-upsert
      {"inventories": [{"manageNumber": "...", "variantId": "...",
                        "mode": "ABSOLUTE", "quantity": 70}, ...]}
      1-400 elements per request; all four fields required on every element.

    PUT  /es/2.0/inventories/manage-numbers/{manageNumber}/variants/{variantId}
      {"mode": "ABSOLUTE", "quantity": 3}      ← no `inventories` array

    Authorization: ESA <Base64(serviceSecret:licenseKey)>
    ABSOLUTE sets, RELATIVE adjusts; ABSOLUTE is required to create a record.
    quantity is a JSON NUMBER (not a string), max 99999.
    manageNumber / variantId are max 32 BYTES.
    SUCCESS IS HTTP 204 WITH NO BODY. Errors carry an `errors` array of
    {code, message, metadata.propertyPath}.
    Roughly one request per second, counted PER REQUEST — so a 400-row batch
    costs the same as a single row.

TWO CONSEQUENCES WORTH STATING

* **204 means there is no per-row result to read.** A bulk request is answered as
  a whole. The spec does not say whether a rejected request applies none, some or
  all of its rows, so `rows_to_reread` exists to name what has to be checked with
  the (confirmed, granted) bulk-get before anything is believed.
* **Validation belongs here, before the request.** A quantity over 99999 or a key
  over 32 bytes would be refused by Rakuten mid-batch, taking the whole request
  with it; refusing locally keeps one bad row from stopping 399 good ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Rakuten's own ceiling for one bulk-upsert request.
MAX_ROWS_PER_REQUEST = 400
#: 在庫数の上限 (SKU単位の仕様表)。
MAX_QUANTITY = 99_999
#: 商品管理番号 / SKU管理番号 の上限。文字数ではなくバイト数。
MAX_KEY_BYTES = 32

MODE_ABSOLUTE = "ABSOLUTE"
MODE_RELATIVE = "RELATIVE"

BULK_UPSERT_PATH = "/es/2.0/inventories/bulk-upsert"
VARIANT_UPSERT_PATH = "/es/2.0/inventories/manage-numbers/{manage_number}/variants/{variant_id}"

#: HTTP status Rakuten answers a successful upsert with. There is no body.
SUCCESS_STATUS = 204


@dataclass(frozen=True, slots=True)
class InventoryRow:
    """One quantity bound for one Rakuten SKU."""

    manage_number: str
    variant_id: str
    quantity: int

    @property
    def key(self) -> tuple[str, str]:
        return (self.manage_number, self.variant_id)


class RowRejected(ValueError):
    """A row Rakuten would refuse. Raised before any request is built."""


def validate(row: InventoryRow) -> None:
    """Refuse locally what Rakuten would refuse remotely.

    Order matters only for the message; every check is independent.
    """
    for label, value in (("商品管理番号", row.manage_number), ("SKU管理番号", row.variant_id)):
        if not value:
            raise RowRejected(f"{label} が空です")
        if len(value.encode()) > MAX_KEY_BYTES:
            raise RowRejected(
                f"{label} が{MAX_KEY_BYTES}バイトを超えています ({value!r} = "
                f"{len(value.encode())}バイト)"
            )
    if row.quantity < 0:
        raise RowRejected(
            f"楽天は負の在庫数を受け付けません ({row.manage_number}/{row.variant_id} = "
            f"{row.quantity})。押し出す前に0で止めてください"
        )
    if row.quantity > MAX_QUANTITY:
        raise RowRejected(
            f"在庫数の上限は{MAX_QUANTITY:,}です ({row.manage_number}/{row.variant_id} = "
            f"{row.quantity:,})"
        )


def build_body(rows: list[InventoryRow], *, mode: str = MODE_ABSOLUTE) -> dict[str, Any]:
    """The bulk-upsert request body, exactly as the spec defines it.

    This is the one function that changes if Rakuten revises the contract, which
    is why it is pure and why the dry-run prints its output verbatim rather than
    a paraphrase of it.
    """
    if not rows:
        raise RowRejected("送信する行がありません")
    if len(rows) > MAX_ROWS_PER_REQUEST:
        raise RowRejected(f"1リクエストは{MAX_ROWS_PER_REQUEST}件までです ({len(rows)}件)")
    for row in rows:
        validate(row)
    return {
        "inventories": [
            {
                "manageNumber": row.manage_number,
                "variantId": row.variant_id,
                "mode": mode,
                # int(), because the spec requires a JSON number and a numeric
                # string is a different thing on the wire.
                "quantity": int(row.quantity),
            }
            for row in rows
        ]
    }


def build_variant_body(row: InventoryRow, *, mode: str = MODE_ABSOLUTE) -> dict[str, Any]:
    """The single-SKU PUT body. The keys travel in the URL, not the body.

    Preferred for the first live write: one SKU, one request, and no way for a
    malformed batch to reach a row we did not intend.
    """
    validate(row)
    return {"mode": mode, "quantity": int(row.quantity)}


def variant_path(row: InventoryRow) -> str:
    validate(row)
    return VARIANT_UPSERT_PATH.format(manage_number=row.manage_number, variant_id=row.variant_id)


def batches(rows: list[InventoryRow]) -> list[list[InventoryRow]]:
    """Split into request-sized chunks.

    The rate limit is per REQUEST, so the whole 587-variant catalogue is two
    requests rather than 587 seconds. Order is preserved so a reviewer can line
    a batch up against the plan that produced it.
    """
    return [
        rows[start : start + MAX_ROWS_PER_REQUEST]
        for start in range(0, len(rows), MAX_ROWS_PER_REQUEST)
    ]


@dataclass(frozen=True, slots=True)
class RakutenError:
    code: str
    message: str
    property_path: str = ""

    def __str__(self) -> str:
        where = f" ({self.property_path})" if self.property_path else ""
        return f"{self.code}: {self.message}{where}".strip()


def parse_errors(body: Any) -> list[RakutenError]:
    """Read Rakuten's `errors` array out of a failure response.

    Deliberately tolerant about SHAPE and strict about MEANING: a body we cannot
    parse yields one error saying so, never an empty list. An empty list here
    would read as "nothing was wrong", which is exactly the misreading that makes
    a rejected write look successful.
    """
    if not isinstance(body, dict):
        return [RakutenError("UNPARSEABLE", f"応答を解釈できません: {body!r:.200}")]
    raw = body.get("errors")
    if not isinstance(raw, list) or not raw:
        return [RakutenError("UNPARSEABLE", f"errors 配列がありません: {body!r:.200}")]
    found: list[RakutenError] = []
    for item in raw:
        if not isinstance(item, dict):
            found.append(RakutenError("UNPARSEABLE", f"{item!r:.100}"))
            continue
        metadata = item.get("metadata")
        path = ""
        if isinstance(metadata, dict):
            path = str(metadata.get("propertyPath") or "")
        found.append(
            RakutenError(
                code=str(item.get("code") or "UNKNOWN"),
                message=str(item.get("message") or ""),
                property_path=path,
            )
        )
    return found


def succeeded(status: int) -> bool:
    """Success is 204 and nothing else.

    A positive test, not the absence of a negative one: any other 2xx means
    Rakuten answered in a way this code was not written against, and treating it
    as success is how a rejected row gets recorded as written.
    """
    return status == SUCCESS_STATUS


def rows_to_reread(rows: list[InventoryRow], status: int) -> list[InventoryRow]:
    """Which rows must be read back with bulk-get before they are believed.

    None after a 204. After anything else, ALL of them: the response carries no
    per-row status, and the spec does not say whether a rejected bulk request
    applies none, some or all of its rows. Assuming "none" would leave a silent
    partial write on live pages.
    """
    return [] if succeeded(status) else list(rows)
