# Build + push container image and deploy to Cloud Run.
#
#   ./scripts/deploy_to_cloud_run.ps1 -Tag phase2-w6-20260917
#
# Requires: gcloud authenticated, project set to inventory-496204.
#
# ORDER MATTERS, and getting it wrong has bitten this project:
#
#   1. scripts/run_db_migration.ps1   schema first, or the new code 500s
#   2. this script                    the endpoints exist only once deployed
#   3. terraform apply                point schedulers at those endpoints
#
# On 2026-09-17 the scheduler was switched to /internal/jobs/shopify-audit
# before the image serving it was deployed, so the job would have 404'd on its
# first run.
#
# AFTER deploying, set `service_image` in infra/terraform/terraform.tfvars to the
# tag used here. The service is dual-managed, and terraform still tracks the
# image (unlike client/client_version/scaling, which run.tf ignores); leaving
# tfvars behind means the next `terraform apply` rolls production back to
# whatever tag it still holds.

param(
    # Immutable, identifiable tag. The default matches the convention already in
    # the registry (phase2-w4-20260903) rather than a fixed version string,
    # which every deploy would otherwise overwrite in place — leaving no way to
    # tell which build is live or to roll back to the previous one.
    [string]$Tag = "phase2-$(Get-Date -Format 'yyyyMMdd-HHmm')"
)

$ErrorActionPreference = "Stop"

$PROJECT_ID = "inventory-496204"
$REGION     = "asia-northeast1"
$REPO       = "product-system"          # Artifact Registry repository
$IMAGE      = "app"
$TAG        = $Tag
$IMG_URL    = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/${IMAGE}:$TAG"

Write-Host "==> Deploying tag: $TAG" -ForegroundColor Cyan

# PowerShell 5.1 wraps every line a native command writes to stderr in an
# ErrorRecord, and under $ErrorActionPreference = "Stop" that aborts the script
# even when the command succeeded with exit code 0. gcloud writes progress and
# informational lines to stderr as a matter of course — "Encryption:
# Google-managed key" from `artifacts repositories describe` killed this deploy
# on 2026-09-17 — so every gcloud call here was a coin flip on how chatty it felt.
#
# Judge native commands by their exit code, which is what actually reports
# success, and keep "Stop" for the cmdlets where it behaves sensibly.
function Invoke-Native {
    param([Parameter(Mandatory = $true)][scriptblock]$Command,
          [Parameter(Mandatory = $true)][string]$What)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & $Command } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

Write-Host "==> Ensuring Artifact Registry repository exists..."
# If the repo is Terraform-managed (manage_artifact_registry=true), this
# describe succeeds and the create is skipped. A non-zero exit is the expected
# "not found" answer here, not a failure, so this one is not wrapped.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
gcloud artifacts repositories describe $REPO --project=$PROJECT_ID --location=$REGION *> $null
$repoExists = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = $prev

if (-not $repoExists) {
    Invoke-Native -What "artifacts repositories create" -Command {
        gcloud artifacts repositories create $REPO `
            --project=$PROJECT_ID `
            --location=$REGION `
            --repository-format=docker `
            --description="Product System container images"
    }
}

Write-Host "==> Submitting build to Cloud Build via cloudbuild.yaml (this takes 3-5 minutes)..."
# Single source of truth for the image build is cloudbuild.yaml (build + push,
# layer-cached). $TAG is passed through as the immutable image tag.
Invoke-Native -What "builds submit" -Command {
    gcloud builds submit `
        --project=$PROJECT_ID `
        --region=$REGION `
        --config=cloudbuild.yaml `
        --substitutions="_TAG=$TAG" `
        .
}

Write-Host "==> Deploying new revision to Cloud Run..."
Invoke-Native -What "run deploy" -Command {
    gcloud run deploy product-system `
        --project=$PROJECT_ID `
        --region=$REGION `
        --image=$IMG_URL `
        --service-account="product-system-app@$PROJECT_ID.iam.gserviceaccount.com" `
        --no-allow-unauthenticated
}

Write-Host "==> Cloud Run service URL:"
gcloud run services describe product-system `
    --project=$PROJECT_ID `
    --region=$REGION `
    --format="value(status.url)"

Write-Host "`nDeployment complete."
Write-Host ""
Write-Host "NEXT, and do not skip it:" -ForegroundColor Yellow
Write-Host "  Set service_image in infra/terraform/terraform.tfvars to:"
Write-Host "    $IMG_URL"
Write-Host "  Otherwise the next terraform apply rolls production back."
