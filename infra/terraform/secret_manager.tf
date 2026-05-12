# Secret slots — populate values OUT OF BAND (gcloud secrets versions add).
# Rotation: replace the secret version; the running Run revision restarts.

resource "google_secret_manager_secret" "rakuten_service_secret" {
  secret_id = "rakuten-service-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.required]
}

resource "google_secret_manager_secret" "rakuten_license_key" {
  secret_id = "rakuten-license-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "shopify_access_token" {
  secret_id = "shopify-access-token"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "shopify_webhook_secret" {
  secret_id = "shopify-webhook-secret"
  replication {
    auto {}
  }
}

# Grant Cloud Run service account access.
locals {
  secret_ids = [
    google_secret_manager_secret.rakuten_service_secret.id,
    google_secret_manager_secret.rakuten_license_key.id,
    google_secret_manager_secret.shopify_access_token.id,
    google_secret_manager_secret.shopify_webhook_secret.id,
  ]
}

resource "google_secret_manager_secret_iam_member" "app_access" {
  for_each  = toset(local.secret_ids)
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.app.email}"
}
