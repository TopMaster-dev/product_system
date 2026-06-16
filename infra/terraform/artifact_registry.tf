# Artifact Registry repository for the product-system container image
# (cloudbuild.yaml pushes here; run.tf / run_jobs.tf pull from here).
#
# Gated behind manage_artifact_registry (default false) because the repo was
# originally created imperatively by scripts/deploy_to_cloud_run.ps1, so it
# already exists in the live project. To bring it under Terraform without a
# 409 on the next apply:
#
#   terraform import \
#     'google_artifact_registry_repository.images[0]' \
#     projects/inventory-496204/locations/asia-northeast1/repositories/product-system
#
# then set manage_artifact_registry = true. Leaving it false keeps Terraform
# hands-off and preserves the existing imperative workflow.

resource "google_artifact_registry_repository" "images" {
  count = var.manage_artifact_registry ? 1 : 0

  location      = var.region
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  description   = "Product System container images"

  depends_on = [google_project_service.required]
}
