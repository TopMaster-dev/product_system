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
