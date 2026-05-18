# Phase 1-A — GCP リソース作成承認依頼

宛先: 株式会社ロスフォード 馬渡様
件名: 環境構築承認のお願い（35リソース・月額試算）

---

## 1. プロジェクト

| 項目 | 値 |
|---|---|
| プロジェクトID | `inventory-496204` |
| リージョン | `asia-northeast1`（東京） |
| 作成主体 | 開発者（Terraform で一括作成） |
| 作成日 | クライアント承認後即時 |

## 2. 作成するリソース一覧（35件）

### 基盤
| カテゴリ | リソース | 個数 | 用途 |
|---|---|---|---|
| API 有効化 | Google Cloud APIs 8種 | 8 | bigquery / cloudbuild / cloudscheduler / cloudtasks / iam / run / secretmanager / sqladmin |
| サービスアカウント | `product-system-app` | 1 | Cloud Run 実行 + Scheduler 呼び出し |

### データベース
| カテゴリ | リソース | 個数 | 詳細 |
|---|---|---|---|
| Cloud SQL Instance | `product-system` (PostgreSQL 15, db-f1-micro) | 1 | 24時間稼働、自動バックアップ ON |
| Database | `product_system` | 1 | アプリ用 DB |
| User | postgres | 1 | パスワード認証 |

### アプリ実行基盤
| カテゴリ | リソース | 個数 | 詳細 |
|---|---|---|---|
| Cloud Run Service | `product-system` | 1 | リクエスト課金、未使用時はゼロスケール |
| IAM Binding | Run Invoker (self), Cloud SQL Client | 2 | 内部呼び出し権限 |

### 非同期処理
| カテゴリ | リソース | 個数 | 詳細 |
|---|---|---|---|
| Cloud Tasks Queue | `product-system-webhook` | 1 | Shopify Webhook 処理用 |
| Cloud Scheduler | bq-export-daily / rakuten-poll / shopify-poll | 3 | 日次BQ Export + 5/15分ポーリング |

### シークレット
| カテゴリ | リソース | 個数 | 詳細 |
|---|---|---|---|
| Secret Manager Secrets | 4枠（楽天 secret/key、Shopify token/webhook secret） | 4 | 値は別途登録 |
| Secret IAM Bindings | 各シークレットに accessor 付与 | 4 | アプリ SA のみ読み取り可 |

### 分析
| カテゴリ | リソース | 個数 | 詳細 |
|---|---|---|---|
| BigQuery Dataset | `product_system`（または既存指定） | 1 | 日次 export 先 |
| BigQuery Tables | master_skus / channel_sku_mappings / orders / order_items / inventory_events / inventory_snapshots | 6 | パーティション設定済 |
| Dataset IAM | アプリ SA に dataEditor 付与 | 1 | 書き込み権限 |

**合計：35リソース**

## 3. 月額費用試算（東京リージョン、Phase 1-A の想定負荷）

| 項目 | 月額（USD） | 月額（円目安） | 備考 |
|---|---|---|---|
| Cloud SQL (db-f1-micro 24/7) | $8〜10 | ¥1,200〜1,500 | 最小スペック。Phase 2 で増強 |
| Cloud Run (リクエスト課金) | $0 | ¥0 | 想定 60 注文/日は無料枠内 |
| Cloud Tasks | $0 | ¥0 | 無料枠 1M req/月 |
| Cloud Scheduler | $0 | ¥0 | ジョブ3個（無料枠5個まで） |
| Secret Manager | $0〜1 | ¥0〜150 | 4 シークレット |
| BigQuery ストレージ | $0〜2 | ¥0〜300 | 初期データ量小 |
| BigQuery クエリ | $0 | ¥0 | 取込のみ、クエリは Phase 2 から |
| Cloud Logging | $0〜1 | ¥0〜150 | ログ量による |
| Cloud Build | $0 | ¥0 | 無料枠 120分/日 |
| Egress / その他 | $0〜1 | ¥0〜150 | 通常運用 |
| **合計** | **$10〜15** | **¥1,500〜2,250** | 仕様予算範囲内 |

※ 為替 1USD=¥150 換算  
※ Phase 1-B（書き戻し）/ 2（分析）/ 3（AI）に進むと SQL スペック・BQ クエリ量が増えます

## 4. セキュリティ前提

| 項目 | 設定 |
|---|---|
| Cloud SQL 接続 | Public IP だが **authorized_networks 空** → 外部直結拒否、Cloud Run からのみ Auth Proxy 経由で接続 |
| SSL モード | `ENCRYPTED_ONLY`（暗号化必須） |
| Secret Manager | アプリ SA のみ accessor 権限、人間が見るには別途権限要 |
| Cloud Run | サービスアカウント認証必須（公開 invoke 不可、Scheduler/Tasks のみ） |
| Webhook 受信 | HMAC-SHA256 検証、不正は 401 + 監査ログ |

## 5. ロールバック

- 全リソースは Terraform 管理 → `terraform destroy` でクリーン削除可
- Cloud SQL は `deletion_protection = true` → 誤削除防止（解除は明示的設定変更が必要）
- データ保全：Cloud SQL 自動バックアップ + BigQuery export（複数経路）

## 6. 承認後の流れ

1. `terraform apply` で 35 リソース作成（所要 10〜15 分）
2. Cloud Build でアプリ Docker イメージビルド → Artifact Registry へ push
3. Cloud Run にデプロイ
4. DB マイグレーション実行（Alembic）
5. Secret Manager に楽天/Shopify の認証情報を登録（**クライアント様にお願いします**）
6. Webhook URL を Shopify に登録
7. サンドボックスで疎通テスト
8. 本番切替

## 7. ご承認いただきたい事項

下記についてご返信ください：

- [ ] **作成リソース 35件・月額 ¥1,500〜2,250 で進めて問題ない**
- [ ] **BigQuery データセット名**：`product_system`（新規作成）／既存指定（→名前と所属プロジェクト）
- [ ] **特記事項**（あれば）：

ご承認をいただきましたら、即時 `terraform apply` を実行いたします。
所要約 15 分で全リソースが立ち上がり、続けてアプリのデプロイに進みます。

何かご不明点がございましたらお気軽にお知らせください。
