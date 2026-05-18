# Secret slots — populate values OUT OF BAND (gcloud secrets versions add).
# Rotation: replace the secret version; the running Run revision restarts.

locals {
  app_secrets = toset([
    "rakuten-service-secret",
    "rakuten-license-key",
    "shopify-access-token",
    "shopify-webhook-secret",
  ])
}

resource "google_secret_manager_secret" "app_secrets" {
  for_each  = local.app_secrets
  secret_id = each.value
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

# Grant Cloud Run service account access to read each secret value.
resource "google_secret_manager_secret_iam_member" "app_access" {
  for_each  = local.app_secrets
  secret_id = google_secret_manager_secret.app_secrets[each.value].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}
