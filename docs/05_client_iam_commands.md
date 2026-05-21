# クライアント様にお願いしたい IAM 設定（3 コマンド）

宛先: 株式会社ロスフォード 馬渡様

GCP の Terraform 構築は完了いたしました。最後に、**プロジェクトオーナー権限が
必要な 3 つの IAM 設定**を、馬渡様（オーナーアカウント）から実行いただく必要があります。

弊側の開発者アカウントは Editor 権限のため、これらの権限付与ができません。
ご対応いただきましたら、本番デプロイに進めます。

---

## 実行コマンド（3 件、合計 1 分以下）

GCP Cloud Shell（ブラウザから https://console.cloud.google.com/ → 上部の Cloud Shell アイコン）で
下記をそのまま貼り付けて実行できます。

```bash
# (1) 楽天/Shopify から認証情報を保管する Cloud SQL へアプリ SA がアクセスできるようにする
gcloud projects add-iam-policy-binding inventory-496204 \
    --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
    --role=roles/cloudsql.client

# (2) Cloud Scheduler のジョブから Cloud Run を呼び出せるようにする
gcloud run services add-iam-policy-binding product-system \
    --project=inventory-496204 \
    --region=asia-northeast1 \
    --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
    --role=roles/run.invoker

# (3) BigQuery データセット product_system にアプリ SA が書き込めるようにする
bq add-iam-policy-binding \
    --project_id=inventory-496204 \
    --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
    --role=roles/bigquery.dataEditor \
    product_system
```

---

## 各コマンドの確認方法（任意）

```bash
# (1) 確認
gcloud projects get-iam-policy inventory-496204 \
    --flatten="bindings[].members" \
    --filter="bindings.role:roles/cloudsql.client AND bindings.members:serviceAccount:product-system-app*" \
    --format="value(bindings.role)"
# → roles/cloudsql.client が表示されれば OK

# (2) 確認
gcloud run services get-iam-policy product-system \
    --project=inventory-496204 --region=asia-northeast1 \
    --format=json | findstr "run.invoker"
# → roles/run.invoker が表示されれば OK

# (3) 確認
bq get-iam-policy --project_id=inventory-496204 product_system | findstr "dataEditor"
# → roles/bigquery.dataEditor が表示されれば OK
```

---

## あわせて削除をお願いしたい Secret Manager の重複シークレット

`gcloud secrets list` を見ると、Terraform で作成した 4 つの他に、
過去に手動で作成された **3 つの重複シークレット**があります：

| 削除対象 | 理由 |
|---|---|
| `Shopify_Webhook_secret` | 弊側は `shopify-webhook-secret`（小文字）を使用 |
| `rakuten` | 弊側は `rakuten-service-secret` / `rakuten-license-key` を使用 |
| `shopify-admin-token` | 弊側は `shopify-access-token` を使用 |

削除コマンド：

```bash
gcloud secrets delete Shopify_Webhook_secret --project=inventory-496204 --quiet
gcloud secrets delete rakuten --project=inventory-496204 --quiet
gcloud secrets delete shopify-admin-token --project=inventory-496204 --quiet
```

弊側で使用するシークレットは下記の 4 つで、これらはそのまま残してください：
- `rakuten-service-secret`
- `rakuten-license-key`
- `shopify-access-token`
- `shopify-webhook-secret`

---

## 完了報告のお願い

上記 6 コマンド（IAM 3 件 + 削除 3 件）を実行いただきましたら、お知らせください。
弊側で続けて以下を実施いたします：

1. アプリの Docker イメージビルド & push（Cloud Build）
2. Cloud Run へ本番イメージのデプロイ
3. PostgreSQL マイグレーション（Alembic）実行
4. ヘルスチェック疎通確認
5. 動作確認後、楽天 / Shopify 認証情報の Secret Manager 登録手順をお送り

ご不明点がございましたらお気軽にお問い合わせください。
