"""Phase 2 W0 health checks that need a DB connection (READ-ONLY).

Invoked by scripts/phase2_w0_health_check.ps1 with the Cloud SQL proxy already
running and DATABASE_URL pointed at it. Writes nothing.

Checks:
  2. bigquery_export_runs over the last 7 days — is the master_skus export failing?
     (The Terraform-pinned BQ schema was missing is_bundle/image_url, and the
     endpoint used to swallow that as HTTP 200 "partial".)
  3. Inventory health — negative stock, and snapshot vs SUM(events) drift, which
     `ReconcileService.approve_diff` can introduce by overwriting the snapshot
     absolutely while writing a scan-time delta.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


async def main() -> int:
    url = os.environ.get("DATABASE_URL")
    if not url or "127.0.0.1:5433" not in url:
        print("DATABASE_URL が proxy (127.0.0.1:5433) を指していません。中止します。")
        return 2
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            print("\n-- 2. BigQuery エクスポート(直近7日) --")
            rows = (
                await conn.execute(
                    text(
                        """
                        SELECT table_name,
                               status,
                               count(*) AS runs,
                               max(started_at) AS latest
                        FROM bigquery_export_runs
                        WHERE started_at > now() - interval '7 days'
                        GROUP BY table_name, status
                        ORDER BY table_name, status
                        """
                    )
                )
            ).all()
            if not rows:
                print("   直近7日の実行記録がありません(スケジューラ未実行の可能性)。")

            # 全期間の成否 — 「一度でも成功したことがあるか」を確定させる
            alltime = (
                await conn.execute(
                    text(
                        """
                        SELECT status, count(*) AS runs,
                               min(started_at) AS first, max(started_at) AS last
                        FROM bigquery_export_runs
                        GROUP BY status ORDER BY status
                        """
                    )
                )
            ).all()
            print("   [全期間]")
            for a in alltime:
                print(f"     {a.status:10} runs={a.runs:<5} {a.first} .. {a.last}")
            # NOTE: the service writes status="success" (app/services/bigquery_export.py:152),
            # NOT "succeeded". Matching the wrong literal here previously reported
            # "never succeeded" for a job that had in fact worked for its first days.
            ok = next((a for a in alltime if a.status == "success"), None)
            if ok is None:
                print("     -> 一度も成功していません。")
            else:
                print(f"     -> 最後の成功: {ok.last}")
                bad = next((a for a in alltime if a.status == "failed"), None)
                if bad is not None and bad.last > ok.last:
                    print(f"        以降ずっと失敗しています(直近 {bad.last})。")
            failed = [r for r in rows if r.status == "failed"]
            for r in rows:
                mark = "  [FAILED]" if r.status == "failed" else ""
                print(
                    f"   {r.table_name:24} {r.status:10} runs={r.runs:<4} latest={r.latest}{mark}"
                )
            if failed:
                print("\n   [要対応] 失敗しているテーブルがあります。")
                print("   bq_schemas の列不足が原因の場合、W0で修正済みの master_skus.json を")
                print("   terraform apply で反映してください。")
                print("   (tests/test_bq_schema_parity.py が恒久ガードになります)")
                err = (
                    await conn.execute(
                        text(
                            """
                            SELECT table_name, error, started_at
                            FROM bigquery_export_runs
                            WHERE status = 'failed'
                            ORDER BY started_at DESC
                            LIMIT 5
                            """
                        )
                    )
                ).all()
                for e in err:
                    print(f"     - {e.table_name} @ {e.started_at}:")
                    print(f"       {str(e.error)[:400]}")
            else:
                print("   [OK] 直近7日に失敗はありません。")

            print("\n-- 3. 在庫の健全性 --")
            neg = (
                await conn.execute(
                    text("SELECT count(*) FROM inventory_snapshots WHERE on_hand_qty < 0")
                )
            ).scalar_one()
            total = (await conn.execute(text("SELECT count(*) FROM master_skus"))).scalar_one()
            print(f"   マスターSKU総数        : {total}")
            print(f"   マイナス在庫のSKU      : {neg}")

            drift = (
                await conn.execute(
                    text(
                        """
                        SELECT count(*) FROM (
                            SELECT s.master_sku_id
                            FROM inventory_snapshots s
                            JOIN (
                                SELECT master_sku_id, COALESCE(sum(quantity_delta), 0) AS total
                                FROM inventory_events GROUP BY master_sku_id
                            ) e ON e.master_sku_id = s.master_sku_id
                            WHERE s.on_hand_qty <> e.total
                        ) d
                        """
                    )
                )
            ).scalar_one()
            print(f"   snapshot != SUM(events): {drift}")
            if drift:
                print("   [参考] リコンサイル承認時の絶対上書きにより発生します(W1で修正予定)。")
                print("          Phase 2 の分析はイベント再生に依存します。")
                print("          着手前の実数を記録しておくこと。")
            else:
                print("   [OK] イベント合計と一致しています。")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
