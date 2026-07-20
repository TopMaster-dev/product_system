# Daily BigQuery export — JST 03:00 = UTC 18:00 of the prior day.
resource "google_cloud_scheduler_job" "bq_export_daily" {
  name        = "product-system-bq-export-daily"
  description = "Daily BigQuery export of all source tables."
  schedule    = "0 18 * * *" # UTC; equivalent to 03:00 JST.
  time_zone   = "Etc/UTC"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/bq-export"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }

  depends_on = [google_project_service.required]
}

# Rakuten polling — every 5 minutes.
resource "google_cloud_scheduler_job" "rakuten_poll" {
  name      = "product-system-rakuten-poll"
  schedule  = "*/5 * * * *"
  time_zone = "Etc/UTC"
  region    = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/poll-rakuten"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }
}

# Shopify polling redundancy — every 15 minutes (webhook is primary).
resource "google_cloud_scheduler_job" "shopify_poll" {
  name      = "product-system-shopify-poll"
  schedule  = "*/15 * * * *"
  time_zone = "Etc/UTC"
  region    = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/poll-shopify"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }
}

# Daily CROSS MALL reconciliation — JST 06:00 = UTC 21:00 of the prior day.
# Reads the stock CSV at settings.reconcile_csv_uri and creates a ReconcileRun
# in pending_approval; the operator approves the diffs in the admin UI (D-6).
# No-ops safely until reconcile_csv_uri is configured.
resource "google_cloud_scheduler_job" "reconcile_daily" {
  name        = "product-system-reconcile-daily"
  description = "Daily CROSS MALL inventory reconciliation (creates a pending-approval run)."
  schedule    = "0 21 * * *" # UTC; equivalent to 06:00 JST.
  time_zone   = "Etc/UTC"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/reconcile"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }

  depends_on = [google_project_service.required]
}

# Batched bundle/shared-stock availability push to Shopify (D-6) — hourly.
# Recomputes each set/anklet-bracelet parent's derived availability and pushes
# it; decoupled from sale ingestion so pushes stay batched and rate-limited.
resource "google_cloud_scheduler_job" "bundle_push_hourly" {
  name        = "product-system-bundle-push-hourly"
  description = "Hourly derived bundle/shared-stock availability push to Shopify."
  schedule    = "0 * * * *"
  time_zone   = "Etc/UTC"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/bundle-push"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }

  depends_on = [google_project_service.required]
}
