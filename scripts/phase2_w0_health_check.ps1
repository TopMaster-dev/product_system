# Phase 2 W0-4: 本番健全性確認（読み取り専用）
#
# Phase 2 に着手する前に、本番が想定どおりの状態かを確認する。すべて READ-ONLY で、
# 書き込み・デプロイは一切行わない。
#
#   powershell -ExecutionPolicy Bypass -File scripts\phase2_w0_health_check.ps1
#
# 確認項目:
#   1. alembic current == 0008 （スキーマドリフトが無いこと）
#   2. bigquery_export_runs の直近7日 — master_skus のエクスポートが失敗していないか
#   3. 在庫の健全性 — マイナス在庫件数・snapshot と events の乖離
#   4. 稼働中の Cloud Run イメージ vs terraform.tfvars の service_image
#   5. Cloud Scheduler ジョブの一覧と状態

$ErrorActionPreference = "Stop"

$PROJECT_ID = "inventory-496204"
$REGION     = "asia-northeast1"
$INSTANCE   = "product-system"
$DB         = "product_system"
$LOCAL_PORT = 5433
$CONN_NAME  = "${PROJECT_ID}:${REGION}:${INSTANCE}"

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

# --- 認証情報 ---------------------------------------------------------------
$tfvars = Get-Content "infra/terraform/terraform.tfvars" -Raw
$DB_PASSWORD = ([regex]'db_password\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
if (-not $DB_PASSWORD) { throw "terraform.tfvars から db_password を読み取れませんでした" }
$TFVARS_IMAGE = ([regex]'service_image\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value

# --- 4/5 は proxy 不要なので先に -------------------------------------------
Section "4. Cloud Run 稼働イメージ vs terraform.tfvars"
$LIVE_IMAGE = (gcloud run services describe product-system `
    --project=$PROJECT_ID --region=$REGION `
    --format="value(spec.template.spec.containers[0].image)").Trim()
Write-Host "  稼働中          : $LIVE_IMAGE"
Write-Host "  terraform.tfvars: $(if ($TFVARS_IMAGE) { $TFVARS_IMAGE } else { '(未設定 -> 既定値のhelloになります)' })"
if ($LIVE_IMAGE -ne $TFVARS_IMAGE) {
    Write-Host "  [要対応] 不一致です。この状態で bare な terraform apply を実行すると本番が巻き戻ります。" -ForegroundColor Yellow
    Write-Host "           terraform.tfvars に service_image = `"$LIVE_IMAGE`" を設定してください。" -ForegroundColor Yellow
} else {
    Write-Host "  [OK] 一致しています。" -ForegroundColor Green
}

Section "5. Cloud Scheduler ジョブ"
gcloud scheduler jobs list --project=$PROJECT_ID --location=$REGION `
    --format="table(name.basename(), schedule, state, lastAttemptTime)"

# --- proxy 起動 -------------------------------------------------------------
Section "Cloud SQL Auth Proxy 起動 (port $LOCAL_PORT)"
$proxyDir = ".local/cloud-sql-proxy"
New-Item -ItemType Directory -Path $proxyDir -Force | Out-Null
if (-not (Test-Path "$proxyDir/cloud-sql-proxy.exe")) {
    Write-Host "  proxy をダウンロードしています..."
    Invoke-WebRequest `
        -Uri "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.13.0/cloud-sql-proxy.x64.exe" `
        -OutFile "$proxyDir/cloud-sql-proxy.exe" -UseBasicParsing
}
$proxy = Start-Process -PassThru -NoNewWindow `
    -FilePath "$proxyDir/cloud-sql-proxy.exe" `
    -ArgumentList @("$CONN_NAME", "--port=$LOCAL_PORT")

try {
    Start-Sleep -Seconds 5
    $env:DATABASE_URL_SYNC = "postgresql+psycopg2://postgres:${DB_PASSWORD}@127.0.0.1:${LOCAL_PORT}/$DB"
    $env:DATABASE_URL      = "postgresql+asyncpg://postgres:${DB_PASSWORD}@127.0.0.1:${LOCAL_PORT}/$DB"

    Section "1. Alembic リビジョン"
    py -m alembic current
    Write-Host "  期待値: 0008 (head)。ずれている場合は Phase 2 着手前に解消すること。"

    Section "2/3. BigQueryエクスポート状況 と 在庫健全性"
    py scripts/phase2_w0_db_checks.py
}
finally {
    Section "Cloud SQL Auth Proxy 停止"
    Stop-Process -Id $proxy.Id -Force -ErrorAction SilentlyContinue
}

Write-Host "`n完了。結果を docs/24 のW0チェック項目に転記してください。" -ForegroundColor Green
