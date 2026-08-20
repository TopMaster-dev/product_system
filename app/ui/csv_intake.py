"""The single CSV-intake helper for every admin upload screen.

Phase 2 adds several CSV uploads (棚卸, カテゴリ, 原価, 入荷) on top of the
existing CROSS MALL stock import. Three independently-written designs each
proposed their own `csv_intake` module with incompatible signatures; whichever
landed second would have rewritten the first's callers and tests. This is that
module, defined once — callers supply a `CsvSpec` and nothing else.

What it handles that every uploader needs:

* **Encoding.** Files come from CROSS MALL (CP932) and from Excel (UTF-8 BOM,
  or CP932 again). Encodings are tried in order and the first that decodes wins.
* **Header aliases.** Staff-maintained sheets never agree on a column name —
  `SKUコード` / `sku_code` / `SKU` all mean the same thing. Aliases are declared
  per column instead of being guessed at the call site.
* **Row-level reporting.** Bad rows are reported with their 1-based line number
  (the number the operator sees in Excel), never silently dropped.

`inspect()` never touches the database: it is the "検証" step of the
upload → 検証 → 確認 → 実行 flow, so the operator sees problems before anything
is written.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

# Excel-on-Windows writes CP932; Excel "CSV UTF-8" writes a BOM; our own exports
# are utf-8-sig. Order matters — utf-8-sig must be tried before cp932, since a
# BOM'd file also "decodes" as cp932 into mojibake rather than raising.
DEFAULT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "cp932")

EMPTY_FILE_MESSAGE = "ファイルが空です。"
NO_HEADER_MESSAGE = "ヘッダー行がありません。"
MISSING_COLUMNS_PREFIX = "必須列がありません: "


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One expected column.

    Two independent questions, deliberately NOT collapsed into one flag:

    `required`   — must the COLUMN exist in the header? Absent -> fatal, the
                   whole upload is rejected.
    `allow_empty` — is an empty VALUE acceptable on a given row? False reports a
                   row issue; True skips the row silently (the column is there,
                   this row just has nothing to say — CROSS MALL exports rows
                   with no stock figure and that is not an operator error).

    `validator` runs only on non-empty values and returns an error message, or
    None when the value is acceptable.
    """

    canonical: str
    aliases: tuple[str, ...] = ()
    required: bool = True
    allow_empty: bool = False
    validator: Callable[[str], str | None] | None = None
    empty_message: str | None = None

    def matches(self, header_cell: str) -> bool:
        cell = header_cell.strip()
        return cell == self.canonical or cell in self.aliases


@dataclass(frozen=True, slots=True)
class CsvSpec:
    columns: tuple[ColumnSpec, ...]
    encodings: tuple[str, ...] = DEFAULT_ENCODINGS
    encoding_message: str = "文字コードを判別できませんでした。"
    max_row_issues: int = 50


class CsvDecodeError(ValueError):
    """The bytes did not decode under any configured encoding."""


def decode_csv(data: bytes, spec: CsvSpec) -> str:
    """Decode using the first encoding that succeeds."""
    for encoding in spec.encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvDecodeError(spec.encoding_message)


def resolve_header(header: list[str], spec: CsvSpec) -> tuple[dict[str, int], list[str]]:
    """Map canonical column name -> column index, plus the names not found.

    Only `required` columns are reported as missing; an absent optional column
    simply yields no index.
    """
    index: dict[str, int] = {}
    for column in spec.columns:
        for position, cell in enumerate(header):
            if column.matches(cell):
                index[column.canonical] = position
                break
    missing = [c.canonical for c in spec.columns if c.required and c.canonical not in index]
    return index, missing


@dataclass(slots=True)
class Inspection:
    fatal: list[str] = field(default_factory=list)
    row_issues: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    valid_rows: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Template-friendly shape (the existing reconcile preview reads dicts)."""
        return {
            "fatal": self.fatal,
            "row_issues": self.row_issues,
            "total_rows": self.total_rows,
            "valid_rows": self.valid_rows,
        }


def inspect(data: bytes, spec: CsvSpec) -> Inspection:
    """Validate without touching the database.

    `fatal` entries block the upload entirely (empty file, undecodable, missing
    required column). `row_issues` are per-row problems the operator can fix;
    those rows are skipped but the rest of the file still imports.
    """
    result = Inspection()
    if not data:
        result.fatal.append(EMPTY_FILE_MESSAGE)
        return result
    try:
        text = decode_csv(data, spec)
    except CsvDecodeError as exc:
        result.fatal.append(str(exc))
        return result

    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result.fatal.append(NO_HEADER_MESSAGE)
        return result

    index, missing = resolve_header(header, spec)
    if missing:
        result.fatal.append(MISSING_COLUMNS_PREFIX + " / ".join(missing))
        return result

    widest = max(index.values(), default=-1)
    for lineno, row in enumerate(reader, start=2):
        if not row or len(row) <= widest:
            continue
        result.total_rows += 1
        if _row_issue(row, index, spec, lineno, result):
            continue
        result.valid_rows += 1
    return result


def _row_issue(
    row: list[str],
    index: dict[str, int],
    spec: CsvSpec,
    lineno: int,
    result: Inspection,
) -> bool:
    """Return True when the row must be skipped. Records at most
    `max_row_issues` issues so a wholly malformed file cannot flood the page."""
    for column in spec.columns:
        position = index.get(column.canonical)
        if position is None:
            continue
        value = row[position].strip()
        if not value:
            if not column.allow_empty:
                _record(result, spec, lineno, column.empty_message or f"{column.canonical}が空です")
            return True
        if column.validator is not None:
            message = column.validator(value)
            if message is not None:
                _record(result, spec, lineno, message)
                return True
    return False


def _record(result: Inspection, spec: CsvSpec, lineno: int, reason: str) -> None:
    if len(result.row_issues) < spec.max_row_issues:
        result.row_issues.append({"line": lineno, "reason": reason})


def iter_rows(data: bytes, spec: CsvSpec) -> Iterator[dict[str, str]]:
    """Yield only the rows that `inspect` would count as valid, keyed by
    canonical column name. Raises `CsvDecodeError` / `ValueError` for the same
    conditions `inspect` reports as fatal, so callers should inspect first."""
    text = decode_csv(data, spec)
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError(NO_HEADER_MESSAGE) from None
    index, missing = resolve_header(header, spec)
    if missing:
        raise ValueError(MISSING_COLUMNS_PREFIX + " / ".join(missing))

    widest = max(index.values(), default=-1)
    throwaway = Inspection()
    for lineno, row in enumerate(reader, start=2):
        if not row or len(row) <= widest:
            continue
        if _row_issue(row, index, spec, lineno, throwaway):
            continue
        yield {name: row[pos].strip() for name, pos in index.items()}


def int_validator(message: str) -> Callable[[str], str | None]:
    """Validator factory for integer columns. `message` is formatted with the
    offending raw value as `{value}`."""

    def _validate(value: str) -> str | None:
        try:
            int(value)
        except ValueError:
            return message.format(value=value)
        return None

    return _validate
