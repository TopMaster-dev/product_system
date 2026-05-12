# Send a single Shopify webhook to the local dev server.
#
# Usage:
#   ./scripts/send_webhook.ps1 -OrderId "DEMO-1" -Sku "10087goldS" -Quantity 2
#   ./scripts/send_webhook.ps1 -OrderId "DEMO-1" -Sku "10087goldS" -Quantity 2 -Cancelled
#   ./scripts/send_webhook.ps1 -OrderId "DEMO-X" -Sku "UNKNOWN-SKU" -Quantity 1   # unmapped
#   ./scripts/send_webhook.ps1 -OrderId "DEMO-Y" -Sku "10087goldS" -Quantity 1 -BadHmac

param(
    [Parameter(Mandatory=$true)] [string]$OrderId,
    [Parameter(Mandatory=$true)] [string]$Sku,
    [int]$Quantity = 1,
    [switch]$Cancelled,
    [switch]$BadHmac,
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [string]$Secret = "local_dev_webhook_secret"
)

$ErrorActionPreference = "Stop"

$cancelledAt = if ($Cancelled) { "2026-05-11T03:00:00Z" } else { $null }

$payload = [ordered]@{
    id = $OrderId
    name = "#$OrderId"
    cancelled_at = $cancelledAt
    fulfillment_status = $null
    created_at = "2026-05-11T02:00:00Z"
    currency = "JPY"
    line_items = @(@{
        id = "L-1"
        sku = $Sku
        quantity = $Quantity
        variant_id = 1
        price = "1000"
    })
}
$json = $payload | ConvertTo-Json -Depth 5 -Compress
$tmp = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllBytes($tmp, [System.Text.Encoding]::UTF8.GetBytes($json))

if ($BadHmac) {
    $sig = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
} else {
    $h = New-Object System.Security.Cryptography.HMACSHA256
    $h.Key = [System.Text.Encoding]::UTF8.GetBytes($Secret)
    $sig = [Convert]::ToBase64String($h.ComputeHash([System.IO.File]::ReadAllBytes($tmp)))
}

$wid = "wh-" + [Guid]::NewGuid().ToString("N").Substring(0,8)
$topic = if ($Cancelled) { "orders/cancelled" } else { "orders/create" }

Write-Host "POST $BaseUrl/webhooks/shopify"
Write-Host "  order_id=$OrderId sku=$Sku qty=$Quantity cancelled=$Cancelled bad_hmac=$BadHmac"
Write-Host "  webhook_id=$wid"

$code = curl.exe -s -o nul -w "%{http_code}" -X POST "$BaseUrl/webhooks/shopify" `
    -H "Content-Type: application/json" `
    -H "X-Shopify-Hmac-Sha256: $sig" `
    -H "X-Shopify-Webhook-Id: $wid" `
    -H "X-Shopify-Topic: $topic" `
    --data-binary "@$tmp"

Remove-Item $tmp -Force
Write-Host "  HTTP $code"
