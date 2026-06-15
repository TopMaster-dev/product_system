"""Generate a dummy CROSS MALL inventory CSV for F1.8 verification.

Per client decision D-7, staging / production verification uses a dummy
CSV rather than the real CROSS MALL export so PII never leaves CROSS MALL.

Produces a CP932 (Shift-JIS) file with the same header CROSS MALL emits
(`区分,商品コード,属性１名,属性２名,在庫数量`) and one row per --sku flag
of the form CODE:QTY. The file is consumed unchanged by
`app/cli/reconcile_inventory.py`.

Usage (PowerShell — Windows operator workstation):
    py scripts/make_dummy_recon_csv.py \\
        --out C:\\tmp\\recon_dummy.csv \\
        --sku 00037c:231 \\
        --sku 0010c:-69 \\
        --sku NEW-VARIANT:0

The output file is overwritten if it exists.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path

CSV_HEADER = ["区分", "商品コード", "属性１名", "属性２名", "在庫数量"]
CSV_ENCODING = "cp932"


@dataclass(frozen=True, slots=True)
class SkuRow:
    code: str
    qty: int


def parse_sku_arg(raw: str) -> SkuRow:
    """Split a `CODE:QTY` argument into a SkuRow. Raises ValueError on a
    malformed value so argparse surfaces a clean error to the operator."""
    if ":" not in raw:
        raise ValueError(f"--sku must be CODE:QTY, got {raw!r}")
    code, _, qty_s = raw.partition(":")
    code = code.strip()
    qty_s = qty_s.strip()
    if not code:
        raise ValueError(f"--sku missing CODE in {raw!r}")
    try:
        qty = int(qty_s)
    except ValueError as exc:
        raise ValueError(f"--sku qty must be an integer in {raw!r}") from exc
    return SkuRow(code=code, qty=qty)


def write_csv(rows: list[SkuRow], out_path: Path) -> None:
    """Write the rows to disk in CROSS MALL's CP932 / 5-column format."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding=CSV_ENCODING, newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for r in rows:
            # 区分 'u' = update, attribute columns empty.
            writer.writerow(["u", r.code, "", "", str(r.qty)])


def parse_args(argv: list[str] | None = None) -> tuple[Path, list[SkuRow]]:
    parser = argparse.ArgumentParser(
        prog="make_dummy_recon_csv",
        description="Build a CP932 dummy CROSS MALL inventory CSV "
        "for F1.8 reconcile_inventory verification (D-7).",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output CSV path. Parent directory is created if missing.",
    )
    parser.add_argument(
        "--sku",
        action="append",
        required=True,
        metavar="CODE:QTY",
        help="One row to emit. Repeat for multiple. Example: --sku 00037c:231 --sku 0010c:-69",
    )
    parsed = parser.parse_args(argv)
    try:
        rows = [parse_sku_arg(raw) for raw in parsed.sku]
    except ValueError as exc:
        parser.error(str(exc))
    return parsed.out, rows


def main(argv: list[str] | None = None) -> int:
    try:
        out_path, rows = parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 2)
    write_csv(rows, out_path)
    sys.stdout.write(f"wrote {len(rows)} rows to {out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
