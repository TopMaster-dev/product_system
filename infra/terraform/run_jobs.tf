# Phase 1-B Stage 0 verification Cloud Run Jobs (docs/14).
#
# These 4 jobs run the scripts/*.py verification entrypoints bundled in the
# service image. They are VERIFICATION-ONLY: never invoked by Scheduler, and
# created only when `create_verification_jobs = true`. Flip the flag back to
# false to tear them down (docs/14 §7).
#
# All env / secret wiring is single-sourced from the same vars and
# google_secret_manager_secret resources the main service uses (run.tf), so a
# job can never drift from the service's real configuration — the failure mode
# the Stage 0 adversarial audit caught when these values were hand-copied into
# docs/14 (wrong DB name, missing slack secret).

# ---- Shared env fragments (mirror run.tf) ----

locals {
  # Jobs use jobs_image when set, else fall back to service_image. This lets a
  # verification image be deployed to the jobs WITHOUT changing the live
  # service (run.tf), whose image stays pinned by var.service_image.
  jobs_image = var.jobs_image != "" ? var.jobs_image : var.service_image

  job_base_env = {
    APP_ENV       = "prod"
    APP_LOG_LEVEL = "INFO"
    APP_TIMEZONE  = "Asia/Tokyo"
    # Jobs run `python scripts/x.py`, which puts /app/scripts (not /app) on
    # sys.path, so `import app` fails. The service masks this via uvicorn's
    # CWD insertion; jobs need it explicit. (The v0.3.1+ image also bakes
    # ENV PYTHONPATH=/app, making this redundant but harmless.)
    PYTHONPATH = "/app"
  }

  job_db_env = {
    GCP_PROJECT_ID    = var.project_id
    GCP_REGION        = var.region
    DATABASE_URL      = "postgresql+asyncpg://postgres:${var.db_password}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
    DATABASE_URL_SYNC = "postgresql+psycopg2://postgres:${var.db_password}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
  }

  job_shopify_env = {
    SHOPIFY_SHOP_DOMAIN = var.shopify_shop_domain
    SHOPIFY_API_VERSION = "2025-04"
  }

  job_rakuten_env = {
    RAKUTEN_SHOP_URL = var.rakuten_shop_url
  }

  job_shopify_secrets = {
    SHOPIFY_ACCESS_TOKEN   = "shopify-access-token"
    SHOPIFY_WEBHOOK_SECRET = "shopify-webhook-secret"
  }

  job_rakuten_secrets = {
    RAKUTEN_SERVICE_SECRET = "rakuten-service-secret"
    RAKUTEN_LICENSE_KEY    = "rakuten-license-key"
  }

  # verify-slack only needs the SLACK_WEBHOOK_URL secret once the client has
  # delivered the URL (D-3) AND a secret version exists. Wiring it before a
  # version exists would make even --mode=empty/invalid runs fail to start,
  # so it is gated behind its own flag.
  job_slack_secrets = var.slack_webhook_secret_ready ? {
    SLACK_WEBHOOK_URL = "slack-webhook-url"
  } : {}

  verification_jobs = {
    "product-system-verify-push" = {
      script     = "scripts/verify_push.py"
      timeout    = "300s"
      needs_db   = true
      plain_env  = merge(local.job_base_env, local.job_db_env, local.job_shopify_env, local.job_rakuten_env)
      secret_env = merge(local.job_shopify_secrets, local.job_rakuten_secrets)
    }
    "product-system-verify-slack" = {
      script     = "scripts/verify_slack.py"
      timeout    = "60s"
      needs_db   = false
      plain_env  = local.job_base_env
      secret_env = local.job_slack_secrets
    }
    "product-system-verify-shopify-meta" = {
      script     = "scripts/verify_shopify_meta.py"
      timeout    = "60s"
      needs_db   = false
      plain_env  = merge(local.job_base_env, local.job_shopify_env)
      secret_env = local.job_shopify_secrets
    }
    "product-system-reconcile-admin" = {
      script     = "scripts/reconcile_admin.py"
      timeout    = "600s"
      needs_db   = true
      plain_env  = merge(local.job_base_env, local.job_db_env)
      secret_env = {}
    }
  }
}

resource "google_cloud_run_v2_job" "verification" {
  for_each = var.create_verification_jobs ? local.verification_jobs : {}

  name                = each.key
  location            = var.region
  deletion_protection = false

  template {
    template {
      service_account = google_service_account.app.email
      max_retries     = 0
      timeout         = each.value.timeout

      containers {
        image   = local.jobs_image
        command = ["python"]
        args    = [each.value.script]

        dynamic "env" {
          for_each = each.value.plain_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "env" {
          for_each = each.value.secret_env
          content {
            name = env.key
            value_source {
              secret_key_ref {
                secret  = google_secret_manager_secret.app_secrets[env.value].secret_id
                version = "latest"
              }
            }
          }
        }

        dynamic "volume_mounts" {
          for_each = each.value.needs_db ? [1] : []
          content {
            name       = "cloudsql"
            mount_path = "/cloudsql"
          }
        }
      }

      dynamic "volumes" {
        for_each = each.value.needs_db ? [1] : []
        content {
          name = "cloudsql"
          cloud_sql_instance {
            instances = [google_sql_database_instance.main.connection_name]
          }
        }
      }
    }
  }

  depends_on = [google_project_service.required]
}

# ---- GCS scratch bucket for reconcile CSVs (F1.8 gs:// input) ----
#
# scripts/reconcile_admin.py start/dry-run accept gs://<bucket>/<obj>; the
# operator uploads a dummy CSV here (docs/14 §4.4). Test scratch space:
# force_destroy + a short lifecycle so dummy CSVs never linger.

resource "google_storage_bucket" "recon_verify" {
  count = var.create_verification_jobs ? 1 : 0

  name                        = var.recon_verify_bucket
  location                    = var.region
  force_destroy               = true
  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_storage_bucket_iam_member" "recon_verify_reader" {
  count = var.create_verification_jobs ? 1 : 0

  bucket = google_storage_bucket.recon_verify[0].name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.app.email}"
}
