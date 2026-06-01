"""Build a human-reviewable spot-check sample of the proposed resolutions,
showing the supporting evidence (Rakuten product name + SKU管理番号 children)
so the reviewer can quickly verify each heuristic-based match."""

from __future__ import annotations

import csv
import random
from pathlib import Path

ROOT = Path(__file__).parent
ENC = "cp932"

# Reproducible sampling (avoid Math.random equivalent; we want determinism)
random.seed(20260601)


def main() -> None:
    # Load Rakuten data for evidence lookups
    with (ROOT / "rakuten" / "normal-item_0601005523_405.csv").open(encoding=ENC, newline="") as f:
        rk = list(csv.reader(f))
    rh = rk[0]
    rk_mgmt = rh.index("商品管理番号（商品URL）")
    rk_name = rh.index("商品名")
    rk_sku_mgmt = rh.index("SKU管理番号")

    names: dict[str, str] = {}
    children: dict[str, list[str]] = {}
    for r in rk[1:]:
        if len(r) <= max(rk_mgmt, rk_name, rk_sku_mgmt):
            continue
        m = r[rk_mgmt]
        if not m:
            continue
        if r[rk_name] and m not in names:
            names[m] = r[rk_name][:80]
        if r[rk_sku_mgmt]:
            children.setdefault(m, []).append(r[rk_sku_mgmt])

    # Load proposed resolutions
    with (ROOT / "_proposed_resolutions.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    # Sample by strategy
    by_strat: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_strat.setdefault(row["strategy"], []).append(row)

    sample_size = {
        "shopify:exact": 3,
        "D:alert+c→product": 6,
        "B:token→product": 9,    # all 9
        "A:sku→variant": 4,      # all 4
        "C:alert→product": 2,    # all 2
        "F:name#token": 2,       # all 2
    }

    print("=" * 100)
    print("SPOT-CHECK SAMPLE — 26 proposed resolutions across all strategies")
    print("=" * 100)

    for strat, picks_needed in sample_size.items():
        candidates = by_strat.get(strat, [])
        if not candidates:
            continue
        picks = candidates if len(candidates) <= picks_needed else random.sample(candidates, picks_needed)
        print(f"\n── strategy: {strat}  (showing {len(picks)} of {len(candidates)}) ──")
        for p in picks:
            ch, cs, ms = p["channel"], p["channel_sku"], p["master_sku"]
            evidence = ""
            if ch == "rakuten":
                nm = names.get(cs, "")
                kids = children.get(cs, [])
                evidence = f"  name={nm!r}  sku_children={kids[:4]}"
            print(f"  {ch:8s} {cs:25s} → {ms:20s}{evidence}")

    print("\n" + "=" * 100)
    print("RESIDUAL — 4 SKUs needing client confirmation")
    print("=" * 100)
    with (ROOT / "_residual_unmapped.csv").open(encoding="utf-8") as f:
        for line in f:
            print("  " + line.rstrip())


if __name__ == "__main__":
    main()
