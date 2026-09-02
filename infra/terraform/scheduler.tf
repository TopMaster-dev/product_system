# Daily BigQuery export — JST 03:00 = UTC 18:00 of the prior day.
resource "google_cloud_scheduler_job" "bq_export_daily" {
  name        = "product-system-bq-export-daily"
  description = "Daily BigQuery export of all source tables."
  schedule    = "0 18 * * *" # UTC; equivalent to 03:00 JST.
  time_zone   = "Etc/UTC"
  region      = var.region

  # The endpoint returns 500 on a per-table failure (it used to return 200
  # "partial", which hid a broken master_skus export for weeks), so a retry is
  # worth having: without retry_config, Cloud Scheduler's HTTP default is 0 and
  # a transient BigQuery error would wait a full day.
  #
  # But retries are not free here. Incremental tables load with WRITE_APPEND,
  # and a process killed between BigQuery accepting a window and Postgres
  # committing it will re-append that window. Windowed exports cap the exposure
  # at one day (it was three months on 2026-08-21, times four attempts), and
  # dropping 3 -> 1 halves what is left. Raise this again only once the loads
  # are idempotent via a staging table + MERGE.
  retry_config {
    retry_count          = 1
    min_backoff_duration = "60s"
    max_backoff_duration = "300s"
  }

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

# --- Phase 2 W4: analytics rollups ---------------------------------------
#
# Two jobs against one endpoint. The hourly one rebuilds only the JST days
# touched since the last success; the nightly one re-walks a trailing window
# unconditionally, catching anything the incremental window could have missed.
#
# They will overlap eventually — a slow hourly run still going at 03:20 — and
# that is fine: the job takes pg_try_advisory_lock and a second run exits
# having done nothing. "Someone else is already doing it" is success.

resource "google_cloud_scheduler_job" "rollup_hourly" {
  name        = "product-system-rollup-hourly"
  description = "Incremental analytics rollup: only the JST days that changed."
  schedule    = "20 * * * *" # :20 past, clear of the :00 bundle push
  time_zone   = "Etc/UTC"
  region      = var.region

  retry_config {
    retry_count          = 3
    min_backoff_duration = "30s"
    max_backoff_duration = "300s"
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/rollup-daily?mode=incremental"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }

  depends_on = [google_project_service.required]
}

# 04:10 JST = 19:10 UTC. After the 03:00 JST BigQuery export so the two do not
# contend for the same shared vCPU, and before the 06:00 JST reconcile so the
# numbers are current when the day's operations begin.
resource "google_cloud_scheduler_job" "rollup_nightly_repair" {
  name        = "product-system-rollup-nightly-repair"
  description = "Trailing-window analytics rollup repair."
  schedule    = "10 19 * * *"
  time_zone   = "Etc/UTC"
  region      = var.region

  retry_config {
    retry_count          = 3
    min_backoff_duration = "60s"
    max_backoff_duration = "600s"
  }

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.app.uri}/internal/jobs/rollup-daily?mode=repair"
    oidc_token {
      service_account_email = google_service_account.app.email
    }
  }

  depends_on = [google_project_service.required]
}
