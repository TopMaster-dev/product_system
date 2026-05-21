# Secret Manager 認証情報登録ガイド（株式会社ロスフォード様向け）

宛先: 株式会社ロスフォード 馬渡様

GCP リソース作成が完了したことに伴い、楽天 / Shopify の認証情報を
Secret Manager に登録いただく手順です。**馬渡様ご自身で登録いただく方式**
（推奨）で、弊側は登録された値の中身を見ることなく、Cloud Run が読み出して利用します。

---

## 登録する 4 つのシークレット枠

Terraform で以下の枠がすでに用意されています（値は空）。それぞれに「バージョン追加」で値を登録します。

| シークレット名 | 用途 | 元の取得元 |
|---|---|---|
| `rakuten-service-secret` | 楽天 RMS の serviceSecret | RMS → 拡張サービス → API設定 |
| `rakuten-license-key` | 楽天 RMS の licenseKey | 同上 |
| `shopify-access-token` | Shopify Custom App の Admin API access token | Shopify管理画面 → アプリ → アプリ開発 |
| `shopify-webhook-secret` | Shopify Webhook 検証用 secret | 同上、Webhook 設定時に発行される値 |

---

## 登録手順（GCP コンソール）

各シークレットについて下記を繰り返します。

1. https://console.cloud.google.com/security/secret-manager?project=inventory-496204 を開く
2. シークレット名（例：`rakuten-service-secret`）をクリック
3. 上部の **「+ 新しいバージョン」** をクリック
4. 「シークレットの値」テキストエリアに値を **コピー&ペースト**
5. 「シークレットのバージョンを追加」をクリック

これだけです。即時有効化されます。Cloud Run は次のリクエスト処理時に新しい値を読み出します。

---

## gcloud CLI で登録する場合（任意）

CLI が好きな方は下記コマンドでも登録できます：

```powershell
# 楽天
echo -n "実際のserviceSecret値" | gcloud secrets versions add rakuten-service-secret --project=inventory-496204 --data-file=-
echo -n "実際のlicenseKey値"     | gcloud secrets versions add rakuten-license-key   --project=inventory-496204 --data-file=-

# Shopify
echo -n "実際のaccess_token値"   | gcloud secrets versions add shopify-access-token  --project=inventory-496204 --data-file=-
echo -n "実際のwebhook_secret値" | gcloud secrets versions add shopify-webhook-secret --project=inventory-496204 --data-file=-
```

PowerShell の場合は `echo -n` の代わりに：
```powershell
"値" | gcloud secrets versions add NAME --project=inventory-496204 --data-file=-
```

---

## 登録できているかの確認

下記のコマンドで、各シークレットに少なくとも 1 つのバージョンが存在するか確認できます：

```powershell
gcloud secrets versions list rakuten-service-secret  --project=inventory-496204
gcloud secrets versions list rakuten-license-key     --project=inventory-496204
gcloud secrets versions list shopify-access-token    --project=inventory-496204
gcloud secrets versions list shopify-webhook-secret  --project=inventory-496204
```

`STATE: ENABLED` の行があれば成功です。値そのものは表示されません（セキュリティ機能）。

---

## セキュリティの保証

- **馬渡様の Google アカウント** からのみ、登録時に値が見えます。
- 弊側の開発者アカウントは **Secret Manager Admin** ロールにより枠作成は可能ですが、値そのものを読み出すには別途明示的なアクセスが必要です。
- 実際に値を使うのは **Cloud Run のサービスアカウント**（`product-system-app@inventory-496204.iam.gserviceaccount.com`）のみで、これはコードからの読み出しに限定されます。
- 値の更新（ローテーション）は同じ「新しいバージョン」操作で行えます。古いバージョンは自動的に非アクティブ化できます。

---

## 登録が完了したらお知らせください

4 つすべての登録が完了しましたら、お知らせください。
弊側で Cloud Run の再デプロイを実施し、本番疎通テストに進みます。

ご不明点はお気軽にお問い合わせください。
