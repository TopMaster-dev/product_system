# Live HTTP webhook smoke test against http://127.0.0.1:8765
# Uses curl.exe to keep request body bytes byte-identical to what we sign.

$ErrorActionPreference = "Stop"
$base = "http://127.0.0.1:8765"
$secret = "local_dev_webhook_secret"

function Sign-Hmac([byte[]]$body) {
    $h = New-Object System.Security.Cryptography.HMACSHA256
    $h.Key = [System.Text.Encoding]::UTF8.GetBytes($secret)
    [Convert]::ToBase64String($h.ComputeHash($body))
}

function Write-Body($orderId, $qty, $path) {
    $obj = [ordered]@{
        id = $orderId
        name = "#$orderId"
        cancelled_at = $null
        fulfillment_status = $null
        created_at = "2026-05-11T02:00:00Z"
        currency = "JPY"
        line_items = @(@{
            id = "L-1"
            sku = "LIVE-SKU-001"
            quantity = $qty
            variant_id = 1
            price = "1000"
        })
    }
    $json = $obj | ConvertTo-Json -Depth 5 -Compress
    [System.IO.File]::WriteAllBytes($path, [System.Text.Encoding]::UTF8.GetBytes($json))
}

$tmp = [System.IO.Path]::GetTempFileName()

Write-Host "[1] valid HMAC ..." -NoNewline
Write-Body "LIVE-001" 2 $tmp
$bytes = [System.IO.File]::ReadAllBytes($tmp)
$sig = Sign-Hmac $bytes
$code = curl.exe -s -o nul -w "%{http_code}" -X POST "$base/webhooks/shopify" `
    -H "Content-Type: application/json" `
    -H "X-Shopify-Hmac-Sha256: $sig" `
    -H "X-Shopify-Webhook-Id: live-wh-1" `
    -H "X-Shopify-Topic: orders/create" `
    --data-binary "@$tmp"
if ($code -ne "200") { throw "Expected 200, got $code" }
Write-Host " 200 OK"

Write-Host "[2] invalid HMAC ..." -NoNewline
$code = curl.exe -s -o nul -w "%{http_code}" -X POST "$base/webhooks/shopify" `
    -H "Content-Type: application/json" `
    -H "X-Shopify-Hmac-Sha256: AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" `
    -H "X-Shopify-Webhook-Id: live-wh-2" `
    -H "X-Shopify-Topic: orders/create" `
    --data-binary "@$tmp"
if ($code -ne "401") { throw "Expected 401, got $code" }
Write-Host " 401 OK"

Write-Host "[3] duplicate webhook_id ..." -NoNewline
$code = curl.exe -s -o nul -w "%{http_code}" -X POST "$base/webhooks/shopify" `
    -H "Content-Type: application/json" `
    -H "X-Shopify-Hmac-Sha256: $sig" `
    -H "X-Shopify-Webhook-Id: live-wh-1" `
    -H "X-Shopify-Topic: orders/create" `
    --data-binary "@$tmp"
if ($code -ne "200") { throw "Expected 200, got $code" }
Write-Host " 200 OK (idempotent)"

Remove-Item $tmp -Force
Write-Host "`nAll webhook scenarios verified."
