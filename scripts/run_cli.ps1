# 本番DBに対して app.cli.* を実行する共通ラッパー（Cloud SQL Auth Proxy 経由）
#
#   powershell -ExecutionPolicy Bypass -File scripts\run_cli.ps1 -Cli sync_shopify_images -DryRun
#   powershell -ExecutionPolicy Bypass -File scripts\run_cli.ps1 -Cli zero_legacy_stock -Args "--all-negative" -DryRun
#
# 目的:
#   Phase 1-B の運用で、プロキシの起動忘れ・$env:DATABASE_URL の設定漏れ・Shopify
#   認証情報の未設定により、本番CLIの実行が繰り返し失敗した（ローカルDBに接続して
#   "database does not exist"、プレースホルダのドメインで 401 など）。
#   本スクリプトは proxy 起動・接続文字列・チャネル認証情報の取得を一括で行う。
#
# 安全策:
#   -DryRun を付けると CLI に --dry-run を渡す。破壊的な操作は必ず
#   「--dry-run の出力をレビュー → 本実行」の2段で行うこと（docs/24 参照）。

param(
    [Parameter(Mandatory = $true)][string]$Cli,
    [string]$Args = "",
    [switch]$DryRun,
    [switch]$WithShopify
)

$ErrorActionPreference = "Stop"

$PROJECT_ID = "inventory-496204"
$REGION     = "asia-northeast1"
$INSTANCE   = "product-system"
$DB         = "product_system"
$LOCAL_PORT = 5433
$CONN_NAME  = "${PROJECT_ID}:${REGION}:${INSTANCE}"

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

$tfvars = Get-Content "infra/terraform/terraform.tfvars" -Raw
$DB_PASSWORD = ([regex]'db_password\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
if (-not $DB_PASSWORD) { throw "terraform.tfvars から db_password を読み取れませんでした" }

# --- proxy -----------------------------------------------------------------
$proxyDir = ".local/cloud-sql-proxy"
New-Item -ItemType Directory -Path $proxyDir -Force | Out-Null
if (-not (Test-Path "$proxyDir/cloud-sql-proxy.exe")) {
    Section "Cloud SQL Auth Proxy をダウンロード"
    Invoke-WebRequest `
        -Uri "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.13.0/cloud-sql-proxy.x64.exe" `
        -OutFile "$proxyDir/cloud-sql-proxy.exe" -UseBasicParsing
}

Section "Cloud SQL Auth Proxy 起動 (port $LOCAL_PORT)"
$proxy = Start-Process -PassThru -NoNewWindow `
    -FilePath "$proxyDir/cloud-sql-proxy.exe" `
    -ArgumentList @("$CONN_NAME", "--port=$LOCAL_PORT")

try {
    Start-Sleep -Seconds 5
    $env:DATABASE_URL = "postgresql+asyncpg://postgres:${DB_PASSWORD}@127.0.0.1:${LOCAL_PORT}/$DB"
    $env:DATABASE_URL_SYNC = "postgresql+psycopg2://postgres:${DB_PASSWORD}@127.0.0.1:${LOCAL_PORT}/$DB"

    # BigQuery 系CLIは GCP_PROJECT_ID / BIGQUERY_DATASET が未設定だと
    # in-memory クライアントにフォールバックし「成功したのに何も書かれない」
    # 状態になる。プロジェクトとデータセットは秘密情報ではないため常に設定する。
    $env:GCP_PROJECT_ID = $PROJECT_ID
    $env:BIGQUERY_DATASET = ([regex]'bigquery_dataset\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
    if (-not $env:BIGQUERY_DATASET) { $env:BIGQUERY_DATASET = "product_system" }

    if ($WithShopify) {
        Section "Shopify 認証情報を Secret Manager から取得"
        # ローカルの .env はプレースホルダ (local.myshopify.com) なので本番値で上書きする。
        # .Trim() は必須: Windows 由来のシークレットは末尾 CR が付き認証が壊れる。
        $env:SHOPIFY_SHOP_DOMAIN = ([regex]'shopify_shop_domain\s*=\s*"([^"]+)"').Match($tfvars).Groups[1].Value
        $env:SHOPIFY_ACCESS_TOKEN = (gcloud secrets versions access latest `
                --secret=shopify-access-token --project=$PROJECT_ID).Trim()
        Write-Host "  shop=$env:SHOPIFY_SHOP_DOMAIN token_len=$($env:SHOPIFY_ACCESS_TOKEN.Length)"
    }

    Section "接続先の確認"
    Write-Host ("  DB -> " + ($env:DATABASE_URL -replace ':[^:@]+@', ':***@'))
    Write-Host ("  BQ -> " + $env:GCP_PROJECT_ID + ":" + $env:BIGQUERY_DATASET)
    py -m alembic current

    $cmd = "py -m app.cli.$Cli"
    if ($DryRun) { $cmd += " --dry-run" }
    if ($Args)   { $cmd += " $Args" }

    Section "実行: $cmd"
    # 参照のみのフラグ (--status など) で「本実行です」と出すと警告が形骸化し、
    # 本当に危険な実行のときに読み飛ばされる。
    # inspect_* は引数なしの参照専用CLI。export_unmapped_worksheet はDBを読み
    # 取ってローカルにCSVを書くだけで、DBもチャネルも変更しない。
    $readOnly = ($Args -match '(^|\s)--(status|list|report)(\s|$)') -or ($Cli -match '^inspect_') -or ($Cli -eq 'export_unmapped_worksheet')
    if (-not $DryRun -and -not $readOnly) {
        Write-Host "  [注意] 本実行です。--dry-run の出力を確認済みであることを前提とします。" -ForegroundColor Yellow
    }
    Invoke-Expression $cmd
}
finally {
    Section "Cloud SQL Auth Proxy 停止"
    Stop-Process -Id $proxy.Id -Force -ErrorAction SilentlyContinue
}
