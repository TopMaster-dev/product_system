# 要件定義書 — 楽天 × Shopify 在庫・需要予測・自動発注システム

**版数**: v1.0
**作成日**: 2026-05-05
**対象フェーズ**: Phase 1-A（縮小版：取り込み + マスター在庫管理）
**作成範囲**: 全Phase要件 / Phase 1-A詳細仕様
**承認**: クライアント確認待ち（合意後ベースライン化、以降は変更管理プロセス）

---

## 0. ドキュメント方針

本書は **Phase 1-A の実装に必要な要件をすべて確定** し、**Phase 1-B / Phase 2 / Phase 3 / Amazon SP-API / 卸販売** までの拡張を見据えた設計境界を定義する。Phase 1-Aの「正確かつ卓越した完成」が後続フェーズ受注の前提であるため、**スコープ外を明示し、変更要求は変更管理プロセスに従う** ものとする。

- **本書で確定するもの**：Phase 1-A の機能 / 非機能 / データモデル / I/F / 画面 / 受入基準
- **本書で方針提示にとどめるもの**：Phase 1-B以降の構造・拡張ポイント（後続フェーズで詳細化）
- **変更管理**：合意後の要件変更は影響範囲・工数・納期への影響を提示し、書面合意で反映する

---

## 1. プロジェクト概要

### 1.1 背景と目的
- 自社EC（楽天市場 + Shopify）の在庫管理〜需要予測〜工場発注までを **段階的に自動化** する。
- 現状は在庫同期が手動運用。手作業の自動化と、**自社資産としてのデータ蓄積基盤** の構築がスタート地点。
- 既存SaaS（クロスモール等）への依存を脱し、最終的には **AIによる需要予測 → 工場発注書ドラフトの自動生成** をゴールとする。

### 1.2 最終ゴール（全Phase完遂時）
1. 楽天・Shopify・Amazon・卸販売の全チャネルからの注文・在庫データを中央DBに集約。
2. 中央DBを Single Source of Truth（SSoT）として、各チャネルへ在庫を双方向同期。
3. 蓄積データから需要予測を行い、工場別発注書ドラフト（PDF + CSV、英/日）を自動生成。
4. 最終承認・送信は人間が実施（AIは草案生成まで）。

### 1.3 段階開発の全体像

| Phase | 内容 | 想定費用 | 想定納期 |
|---|---|---|---|
| **1-A** | Shopify+楽天 取り込み・中央DB構築（書き戻しなし） | 38万円 | 3〜4週間 |
| 1-B | Shopify+楽天 書き戻し・双方向同期 | 25〜30万円 | 3〜4週間 |
| 2 | 在庫分析・欠品予測・発注アラート | 35〜45万円 | 4〜5週間 |
| 3 | AI需要予測・発注書生成（要：6ヶ月以上のデータ蓄積） | 50〜70万円 | 6〜8週間 |
| Amazon | Amazon SP-API連携 | 25〜35万円 | 3〜4週間 |
| 卸 | CSV/Sheets/手動入力 取り込み | 15〜25万円 | 2〜3週間 |

**全Phase合算概算**：188〜243万円

---

## 2. Phase 1-A スコープ

### 2.1 IN（含む）
- 楽天RMS APIによる注文の自動取り込み（**Polling方式**）
- Shopify Admin APIによる注文の自動取り込み（**Webhook + Polling 冗長化、HMAC検証必須**）
- 中央在庫DB（PostgreSQL on Cloud SQL）の構築 — マスターSKU中心の正規化テーブル設計
- SKU単位の在庫減算処理（**イベントソーシング型 / 行ロックによる整合性担保**）
- 管理画面（3〜4画面）：SKUマッピング / 在庫一覧 / 手動調整 / イベントログ閲覧
- BigQueryへの **日次データ export**（注文・在庫イベント・在庫スナップショット）
- GCP環境構築：Cloud Run + Cloud Tasks + Secret Manager + Cloud SQL + BigQuery
- 基本的な監視・ログ・エラー通知設計

### 2.2 OUT（含まない／後続フェーズ）
- ❌ 中央DB → 各チャネルへの **在庫書き戻し**（Phase 1-B）
- ❌ 楽天 ↔ Shopify の **双方向自動同期**（Phase 1-B / 当面は既存の別手段で継続）
- ❌ 同期エラー画面・再実行機能・Slack通知（Phase 1-B）
- ❌ 在庫分析ダッシュボード・欠品予測（Phase 2）
- ❌ AI需要予測・発注書生成（Phase 3）
- ❌ Amazon SP-API連携 / 卸販売取り込み（別Phase）
- ❌ 既存システムからの初期データ移行（別途見積）
- ❌ 楽天店舗・Shopifyストアが複数ある場合の追加対応（別途見積：1店舗を前提）
- ❌ GCPの月額運用費（クライアント負担、月3,000〜10,000円程度想定）

### 2.3 前提・制約
- 楽天店舗：**1店舗** を前提（複数店舗は別途見積）
- Shopifyストア：**1ストア** を前提（複数ストアは別途見積）
- 倉庫：**単一倉庫** を前提（複数倉庫管理はPhase 1-B以降で別途）
- 楽天 ↔ Shopify の在庫同期は本Phase期間中も既存手段で継続（クライアント側責務）
- GCPプロジェクト・課金アカウントはクライアントが用意
- 楽天RMS API / Shopify Admin API の認証情報・店舗権限はクライアントから提供
- BigQuery環境は既存のものを使用

---

## 3. ステークホルダー

| 役割 | 担当 | 主な関与 |
|---|---|---|
| 発注者 | クライアント | 要件確定・受入承認・運用責任 |
| 開発者 | 受注者（当方） | 設計・実装・試験・納品・3ヶ月瑕疵対応 |
| 運用担当 | クライアント側EC運用者 | 管理画面の日常運用・SKUマッピング作業 |
| データ利用者 | クライアント側分析担当 | BigQueryからの分析（Phase 2以降） |

---

## 4. 機能要件（Phase 1-A）

### 4.1 楽天注文取り込み（Polling）

| ID | 要件 | 詳細 |
|---|---|---|
| F-RAK-01 | 注文一覧定期取得 | 楽天RMS `getOrder` 系APIで一定間隔（既定：5分）に新規・更新注文を取得 |
| F-RAK-02 | 増分取得 | 前回成功時刻以降の更新注文のみ取得し、API消費を最小化 |
| F-RAK-03 | 注文詳細取得 | 注文番号ごとに明細（SKU・数量・金額）を取得 |
| F-RAK-04 | 重複排除 | `(channel='rakuten', channel_order_id)` をUNIQUE制約で重複登録防止 |
| F-RAK-05 | キャンセル処理 | キャンセル状態に遷移した注文は在庫を戻入（補償イベント追加） |
| F-RAK-06 | リトライ | 一時的失敗（429/5xx）は指数バックオフでリトライ（最大5回） |
| F-RAK-07 | レート制限遵守 | 楽天API制限を超えないよう Token Bucket でスロットリング |
| F-RAK-08 | 認証情報管理 | Secret Manager に格納、コードには平文を持たせない |

### 4.2 Shopify注文取り込み（Webhook + Polling冗長化）

| ID | 要件 | 詳細 |
|---|---|---|
| F-SHO-01 | Webhook受信 | `orders/create`, `orders/updated`, `orders/cancelled` を受信 |
| F-SHO-02 | HMAC検証 | `X-Shopify-Hmac-Sha256` を **必ず検証**、失敗時は401で破棄しログ記録 |
| F-SHO-03 | 即時ACK | Webhook受信は5秒以内に200を返し、処理はCloud Tasksへ非同期投入 |
| F-SHO-04 | Polling冗長化 | Webhook欠落に備え、定期Polling（既定：15分）で `updated_at_min` 増分取得 |
| F-SHO-05 | 重複排除 | `(channel='shopify', channel_order_id)` および Webhook `X-Shopify-Webhook-Id` で冪等処理 |
| F-SHO-06 | キャンセル処理 | 楽天と同様に補償イベントで在庫戻入 |
| F-SHO-07 | GraphQL推奨 | Admin API は **GraphQL** を優先（コスト効率・将来の互換性） |
| F-SHO-08 | 認証情報管理 | Secret Manager 管理、ローテーション可能な構造 |

### 4.3 中央在庫DB（マスターSKU中心の正規化）

| ID | 要件 | 詳細 |
|---|---|---|
| F-DB-01 | master_skus | 自社管理の正規SKU。すべての在庫変動の基準 |
| F-DB-02 | channel_sku_mappings | (master_sku_id, channel, channel_sku, channel_product_id) の対応表、UNIQUE: (channel, channel_sku) |
| F-DB-03 | inventory_events | 在庫変動の **追記専用ログ**（イベントソーシング）。type: order_consumed / cancellation_returned / manual_adjust / stocktake / receipt |
| F-DB-04 | inventory_snapshots | パフォーマンス用にmaster_sku単位の現在在庫を保持。inventory_events から再構築可能（真実はイベント側） |
| F-DB-05 | orders / order_items | 各チャネルから取り込んだ注文の正規化表 |
| F-DB-06 | mapping_alerts | 未マッピングSKU検出時にアラート登録（管理画面で対応） |
| F-DB-07 | 拡張カラム先行用意 | `fulfillment_type`（FBA/MFN/自社等）、`marketplace_id`（JP/US/EU等）を **初期から保持**。後付けマイグレーション回避 |
| F-DB-08 | 整合性 | UNIQUE制約 / 外部キー / NOT NULL を厳格定義。在庫減算は **行ロック（SELECT FOR UPDATE）** で並行制御 |

### 4.4 在庫減算ロジック（イベントソーシング）

| ID | 要件 | 詳細 |
|---|---|---|
| F-INV-01 | 減算契機 | 注文取り込み時に order_items 単位で `order_consumed` イベント追加 |
| F-INV-02 | 冪等性 | 同一 (channel, channel_order_id, line_id) からのイベントは重複登録しない（UNIQUE制約） |
| F-INV-03 | マッピング欠落 | channel_sku が未マッピングの場合は減算せず mapping_alerts に登録、注文は orders に保留状態で記録 |
| F-INV-04 | 取り消し補償 | キャンセル受信時は `cancellation_returned` イベントで打ち消し（イベント削除はしない） |
| F-INV-05 | 手動調整 | 管理画面からの調整は `manual_adjust` イベント（理由・操作者を記録） |
| F-INV-06 | 現在庫導出 | inventory_events の合算 = inventory_snapshots と一致することを日次で検証 |
| F-INV-07 | トランザクション | 注文取込→減算→マッピング解決は単一トランザクション（部分反映禁止） |

### 4.5 管理画面（3〜4画面）

実装方式：**FastAPI + Jinja2 + Tailwind** を第一候補（Retool は要件次第で代替提案）。認証はGCP IAM連動またはBASIC認証＋IP制限から運用に合わせて選定。

| ID | 画面 | 要件 |
|---|---|---|
| F-UI-01 | SKUマッピング画面 | master_sku ↔ channel_sku の一覧・検索・新規作成・編集・削除。CSVインポート/エクスポート |
| F-UI-02 | 在庫一覧画面 | master_sku 単位の現在在庫・直近変動を一覧。SKU検索、低在庫フィルタ |
| F-UI-03 | 手動調整画面 | master_sku を指定して数量・理由を入力、`manual_adjust` イベント発行 |
| F-UI-04 | イベントログ閲覧 | inventory_events の時系列一覧、フィルタ（SKU・期間・type・channel）、詳細表示 |
| F-UI-05 | 認証 | 管理者ログイン必須、操作ログ記録 |

### 4.6 BigQuery 日次 Export

| ID | 要件 | 詳細 |
|---|---|---|
| F-BQ-01 | 対象テーブル | orders, order_items, inventory_events, inventory_snapshots, channel_sku_mappings, master_skus |
| F-BQ-02 | 実行 | Cloud Scheduler → Cloud Run（または Cloud Tasks）で日次 1回（例：JST 03:00） |
| F-BQ-03 | 方式 | 増分 export（updated_at ベース）+ snapshots は日次 full snapshot |
| F-BQ-04 | パーティション | event_date / order_date での日付パーティション |
| F-BQ-05 | リトライ | 失敗時は次回起動でキャッチアップ、3日連続失敗で通知 |
| F-BQ-06 | スキーマ管理 | テーブル定義をコード管理（Terraform または SQL DDLファイル） |

---

## 5. 非機能要件

### 5.1 性能
- 注文取り込み遅延：Webhook起点で **平均5分以内** に在庫反映、Polling起点で **15分以内**
- 管理画面応答：通常画面 **2秒以内**、検索 **5秒以内**（SKU 5万件想定）
- 1日の取り込み想定：注文 5,000件 / 在庫イベント 20,000件まで処理可能

### 5.2 可用性・信頼性
- Cloud Run のヘルスチェック有効化、自動再起動
- Webhook受信は5秒以内応答、処理は Cloud Tasks に積んで非同期化
- 注文取り込みの **At-least-once + 冪等性** で欠落・重複に耐える
- 日次の整合性検証バッチ（events 合算 = snapshots）

### 5.3 セキュリティ
- 全シークレットは **Secret Manager**、コード/環境変数に直書き禁止
- Shopify Webhook の **HMAC検証必須**
- 管理画面は認証必須、操作ログ記録
- DB通信はSSL強制、Cloud SQL の Private IP / IAM認証検討
- 最小権限のサービスアカウント設計

### 5.4 監査・運用
- 構造化ログ（JSON）を Cloud Logging に出力
- エラー発生時は Cloud Logging Alert → メール通知（Slackは Phase 1-B）
- リクエストID／取込バッチIDで横断追跡可能
- DB日次バックアップ（Cloud SQL自動バックアップ）

### 5.5 拡張性（将来Phase対応）
- **Adapter層**：`ChannelAdapter` 抽象基底クラス（`fetch_orders` / `push_inventory` / `verify_webhook`）。Phase 1-Aでは `RakutenAdapter` `ShopifyAdapter` を実装、`push_inventory` はPhase 1-Bで実装
- **チャネル追加コスト最小化**：コア在庫ロジックは Adapter 経由でのみチャネルに触れる
- **fulfillment_type / marketplace_id** を初期スキーマに含め、Amazon追加時のマイグレーションを回避
- 注文ステータスは Adapter 層で **統一ステータス** に正規化

### 5.6 コーディング・品質
- Python 3.11+ / 型ヒント必須 / `ruff` + `mypy` でCI検査
- ユニットテスト：在庫整合性ロジック・冪等性周りは **必須カバー**
- 統合テスト：楽天/Shopify サンドボックスまたはモックで主要シナリオ
- AIコーディングツール（Claude Code / Cursor / Copilot）使用可。ただし **在庫整合性ロジックは人間レビュー必須**、ケース漏れは人間検証、API挙動は実環境で動作確認

---

## 6. 技術スタック（確定）

| 層 | 技術 | 備考 |
|---|---|---|
| 言語 | Python 3.11+ | |
| Webフレームワーク | FastAPI | 非同期I/O、OpenAPI自動生成 |
| DB | PostgreSQL 15+ on Cloud SQL | 行ロック・UNIQUE制約活用 |
| 実行基盤 | Cloud Run | コンテナベース、自動スケール |
| 非同期ジョブ | Cloud Tasks | Webhook処理・Polling・BQ Export |
| 定期実行 | Cloud Scheduler | Polling・日次バッチ起動 |
| シークレット | Secret Manager | API key / DB credential |
| 分析基盤 | BigQuery（既存） | 日次 export 先 |
| 管理画面 | FastAPI + Jinja2 + Tailwind | 第一候補。Retool は要件次第で代替提案 |
| マイグレーション | Alembic | スキーマバージョン管理 |
| IaC | Terraform（推奨） | GCPリソース定義 |
| CI/CD | GitHub Actions（想定） | テスト・デプロイ自動化 |

---

## 7. データモデル概要（Phase 1-A）

```
master_skus
  id (PK), sku_code (UNIQUE), name, jan_code, attributes(JSONB),
  created_at, updated_at

channel_sku_mappings
  id (PK), master_sku_id (FK), channel, channel_sku, channel_product_id,
  marketplace_id (NULL可、Amazon用), fulfillment_type (NULL可、FBA/MFN/自社),
  is_active, created_at, updated_at
  UNIQUE (channel, channel_sku, marketplace_id)

orders
  id (PK), channel, channel_order_id, marketplace_id,
  status (normalized), ordered_at, raw_payload(JSONB),
  created_at, updated_at
  UNIQUE (channel, channel_order_id)

order_items
  id (PK), order_id (FK), line_id, channel_sku, master_sku_id (FK, NULL可),
  quantity, unit_price, currency, fulfillment_type
  UNIQUE (order_id, line_id)

inventory_events
  id (PK), master_sku_id (FK), event_type, quantity_delta,
  source_channel, source_order_id, source_line_id,
  reason, operator, occurred_at, created_at
  UNIQUE (event_type, source_channel, source_order_id, source_line_id)
    -- 冪等性担保（manual_adjust 等の任意イベントには別UNIQUE戦略）

inventory_snapshots
  master_sku_id (PK), on_hand_qty, last_event_id (FK),
  updated_at

mapping_alerts
  id (PK), channel, channel_sku, channel_product_id, first_seen_at,
  occurrence_count, status (open/resolved), resolved_master_sku_id (FK),
  resolved_at

webhook_logs
  id (PK), channel, webhook_id, topic, hmac_valid,
  payload(JSONB), received_at, processed_at, status
  UNIQUE (channel, webhook_id)
```

> 詳細なDDL・インデックス設計は次工程「アーキテクチャ設計書」で確定。

---

## 8. 受入基準（Phase 1-A）

以下を **すべて** 満たすことで Phase 1-A 完了とする。後続フェーズはこの完了をもって着手判断される。

### 8.1 機能受入
- [ ] 楽天サンドボックス／本番にて、注文1件が **平均15分以内** に中央DBへ反映され、対応する `inventory_events.order_consumed` が記録される
- [ ] Shopify Webhook 経由で注文1件が **平均5分以内** に在庫反映、HMAC不正リクエストは401で拒否されログに残る
- [ ] Shopify Webhook 欠落時も Polling で **24時間以内** に必ず取り込まれる
- [ ] 同一注文の Webhook が2回送られても在庫が二重に減らない（冪等性）
- [ ] キャンセル受信で `cancellation_returned` イベントが追加され、在庫スナップショットが回復する
- [ ] 未マッピングSKUの注文は mapping_alerts に登録され、管理画面から master_sku に紐付け解決できる（解決後は手動再処理または自動再処理が可能）
- [ ] 管理画面 4画面（SKUマッピング / 在庫一覧 / 手動調整 / イベントログ閲覧）が動作する
- [ ] 手動調整は理由と操作者がイベントに記録される
- [ ] 日次でBigQueryに対象テーブルがexportされ、当日分が翌日朝までに参照可能

### 8.2 整合性受入
- [ ] inventory_events の合算と inventory_snapshots が一致することを日次で自動検証し、不一致時は通知される
- [ ] 並行注文（同一SKUへの同時注文）でも在庫がマイナスにならず、行ロックで直列化される
- [ ] 冪等性UNIQUE制約により、同一イベントは再送しても二重登録されない

### 8.3 非機能受入
- [ ] すべてのシークレットがSecret Manager管理（リポジトリgrepで .env や apikey 直書きなしを確認）
- [ ] Cloud Run / Cloud SQL / Cloud Tasks / Cloud Scheduler / Secret Manager / BigQuery が IaC（Terraform）で再現可能
- [ ] CIで `ruff` `mypy` `pytest` が通る
- [ ] 在庫整合性ロジックのユニットテストカバレッジ 80%以上
- [ ] 主要ユースケースの統合テスト（モックまたはサンドボックス）が通る

### 8.4 ドキュメント納品物
- [ ] 要件定義書（本書、最終版）
- [ ] アーキテクチャ設計書（次工程で作成）
- [ ] DBスキーマ仕様（DDL + ER図）
- [ ] API仕様書（OpenAPI）
- [ ] 管理画面マニュアル
- [ ] 運用手順書（デプロイ・障害時対応・ローテーション）
- [ ] ソースコード一式（GitHubリポジトリ）

### 8.5 拡張性受入（将来Phase対応の確認）
- [ ] `ChannelAdapter` 抽象クラスが実装されており、`push_inventory` が未実装でも Phase 1-B でメソッド実装のみで対応可能な構造
- [ ] `fulfillment_type` / `marketplace_id` カラムがスキーマに存在
- [ ] 新Adapter追加時、コア在庫ロジックの改修が不要であること（コードレビューで確認）

---

## 9. リスクと対応

| # | リスク | 影響 | 対応 |
|---|---|---|---|
| R1 | 楽天RMS APIの仕様差異・特殊在庫（項目選択肢別在庫等） | 取り込み欠落 | Phase 1-Aは標準SKU構造を前提。特殊在庫はPhase 1-B別途見積 |
| R2 | Shopify Webhook 欠落 | 在庫ズレ | Polling 冗長化で吸収、24h以内に必ず追従 |
| R3 | 既存運用との並行期間の在庫ズレ | 売り越し | Phase 1-Aは書き戻しなしのため、既存同期手段は継続。中央DBは「観測専用」と位置付け |
| R4 | マッピング作業の運用負荷 | 取り込み遅延 | アラート画面 + CSV一括登録機能で運用効率化 |
| R5 | API レート制限 | 取り込み停止 | チャネル別 Token Bucket、指数バックオフ |
| R6 | クライアント側の店舗数前提変更（複数店舗化） | 工数増 | 1店舗前提を明記、追加は別途見積 |
| R7 | AIコーディング起因のロジック欠陥 | 在庫整合性破綻 | 整合性ロジックは人間レビュー必須・テスト網羅 |

---

## 10. 体制・進め方

### 10.1 マイルストーン（Phase 1-A：3〜4週間想定）

| 週 | 主な作業 | 成果物 |
|---|---|---|
| W1 | 要件確定・アーキテクチャ設計・DB設計・GCP環境準備 | アーキテクチャ設計書 / DDL / Terraform 雛形 |
| W2 | Adapter基盤・Shopify取込（Webhook+Polling）・在庫イベント・主要API | コア機能 動作 |
| W3 | 楽天取込・管理画面・BigQuery export・統合テスト | 管理画面 4画面 / E2E通る |
| W4 | サンドボックス→本番疎通・整合性検証・ドキュメント整備・受入試験 | 全受入基準クリア |

### 10.2 コミュニケーション
- 進捗：週次レポート（GitHub Projects または Notion）
- 質疑：チャットで随時、判断を要する事項はメールで合意エビデンス
- レビュー：節目（W1末・W2末・W3末・W4末）で動作デモ

### 10.3 瑕疵対応
- 納品後 **3ヶ月** は受入基準を満たさない不具合を無償対応
- 仕様変更・運用相談・運用代行は別途月額契約で対応

---

## 11. 変更管理

- 本書合意後の要件追加・変更は変更要求書（CR）で受領し、影響範囲・追加工数・納期影響を提示、書面合意のうえ反映する
- スコープ外項目（4章 OUT 列挙、章2.2）は原則 Phase 1-B 以降に持ち越し

---

## 12. 用語集

| 用語 | 意味 |
|---|---|
| マスターSKU | 自社管理の正規SKU。全チャネル共通の真実 |
| ChannelAdapter | 各チャネル接続を抽象化する共通インターフェース |
| イベントソーシング | 在庫変動を追記専用ログとして記録し、現在状態を導出する設計 |
| SSoT | Single Source of Truth。中央DBが在庫の唯一の真実 |
| FBA / MFN | Amazon の Fulfillment by Amazon / Merchant Fulfilled Network |
| 冪等性 | 同じ操作を何度実行しても結果が変わらない性質 |
| HMAC | Webhook送信元の真正性検証に使う署名アルゴリズム |

---

## 13. 次工程

本書の合意を経て、次は以下を作成する：

1. **アーキテクチャ設計書**（コンポーネント図 / シーケンス図 / GCPリソース構成）
2. **DBスキーマ仕様書**（DDL / ER図 / インデックス / 制約）
3. **API仕様書**（OpenAPI 3.0）
4. **画面仕様書**（ワイヤーフレーム / 操作フロー）

合意後、W1からの実装に着手する。
