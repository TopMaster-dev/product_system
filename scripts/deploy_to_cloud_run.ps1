# Build + push container image and deploy to Cloud Run.
# Run AFTER `terraform apply` has finished.
#
#   ./scripts/deploy_to_cloud_run.ps1
#
# Requires: gcloud authenticated, project set to inventory-496204.

$ErrorActionPreference = "Stop"

$PROJECT_ID = "inventory-496204"
$REGION     = "asia-northeast1"
$REPO       = "product-system"          # Artifact Registry repository
$IMAGE      = "app"
$TAG        = "v0.1.0"
$IMG_URL    = "$REGION-docker.pkg.dev/$PROJECT_ID/$REPO/${IMAGE}:$TAG"

Write-Host "==> Ensuring Artifact Registry repository exists..."
# If the repo is Terraform-managed (manage_artifact_registry=true), this
# describe succeeds and the create is skipped.
$exists = gcloud artifacts repositories describe $REPO --project=$PROJECT_ID --location=$REGION 2>$null
if (-not $exists) {
    gcloud artifacts repositories create $REPO `
        --project=$PROJECT_ID `
        --location=$REGION `
        --repository-format=docker `
        --description="Product System container images"
}

Write-Host "==> Submitting build to Cloud Build via cloudbuild.yaml (this takes 3-5 minutes)..."
# Single source of truth for the image build is cloudbuild.yaml (build + push,
# layer-cached). $TAG is passed through as the immutable image tag.
gcloud builds submit `
    --project=$PROJECT_ID `
    --region=$REGION `
    --config=cloudbuild.yaml `
    --substitutions="_TAG=$TAG" `
    .

Write-Host "==> Deploying new revision to Cloud Run..."
gcloud run deploy product-system `
    --project=$PROJECT_ID `
    --region=$REGION `
    --image=$IMG_URL `
    --service-account="product-system-app@$PROJECT_ID.iam.gserviceaccount.com" `
    --no-allow-unauthenticated

Write-Host "==> Cloud Run service URL:"
gcloud run services describe product-system `
    --project=$PROJECT_ID `
    --region=$REGION `
    --format="value(status.url)"

Write-Host "`nDeployment complete."
Write-Host "Next: run scripts/run_db_migration.ps1 to apply Alembic migrations."
