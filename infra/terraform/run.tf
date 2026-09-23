resource "google_service_account" "app" {
  account_id   = "product-system-app"
  display_name = "Product System service account"
}

resource "google_cloud_run_v2_service" "app" {
  name     = "product-system"
  location = var.region

  # Allow terraform to recreate the service if the image / config changes
  # incompatibly. Cloud Run is stateless so destroy+create is safe.
  deletion_protection = false

  # This service is dual-managed: terraform defines it, and releases go out with
  # `gcloud run deploy` (scripts/deploy_to_cloud_run.ps1). The three attributes
  # below are therefore always drifted, and they surfaced in the plan for an
  # unrelated scheduler change on 2026-09-17 — `-target` pulls in the resources
  # a target depends on, and every scheduler job depends on this service for its
  # URI. A plan that always carries an unexplained service update is a plan
  # people stop reading.
  #
  #   client / client_version  Cloud Run records which tool last deployed. gcloud
  #                            writes its own values; terraform wants them back.
  #                            Cosmetic, and it never converges.
  #   scaling                  Present on the live service, absent from this
  #                            config, so terraform proposes REMOVING it.
  #
  # Ignoring preserves whatever is live, which is the safe direction for all
  # three. Set scaling here explicitly if it ever needs to be managed as code.
  lifecycle {
    ignore_changes = [client, client_version, scaling]
  }

  template {
    service_account = google_service_account.app.email

    containers {
      image = var.service_image

      # ---- App basics ----
      env {
        name  = "APP_ENV"
        value = "prod"
      }
      env {
        name  = "APP_LOG_LEVEL"
        value = "INFO"
      }
      env {
        name  = "APP_TIMEZONE"
        value = "Asia/Tokyo"
      }

      # ---- Database (via Cloud SQL Auth Proxy socket) ----
      env {
        name  = "DATABASE_URL"
        value = "postgresql+asyncpg://postgres:${var.db_password}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
      }
      env {
        name  = "DATABASE_URL_SYNC"
        value = "postgresql+psycopg2://postgres:${var.db_password}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
      }

      # ---- Task queue / GCP wiring ----
      env {
        name  = "TASK_QUEUE_BACKEND"
        value = "cloud_tasks"
      }
      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "GCP_REGION"
        value = var.region
      }
      env {
        name  = "BIGQUERY_DATASET"
        value = var.bigquery_dataset
      }
      env {
        name  = "CLOUD_TASKS_QUEUE"
        value = google_cloud_tasks_queue.webhook.name
      }
      env {
        name  = "CLOUD_TASKS_INVOKER_SA"
        value = google_service_account.app.email
      }
      env {
        name  = "CLOUD_TASKS_TARGET_URL"
        value = "https://product-system-4691219310.asia-northeast1.run.app/internal/jobs/tasks/run"
      }
      # `allUsers` holds run.invoker for the Shopify webhooks (see below), which
      # leaves /internal/jobs/* reachable too. Cloud Scheduler and Cloud Tasks
      # both send an OIDC token signed as CLOUD_TASKS_INVOKER_SA;
      # app/api/auth_internal.py verifies it.
      #
      # Shipped as "audit" on 2026-09-22 and promoted to "enforce" on
      # 2026-09-23 on the evidence, not on a schedule: 14 days of production
      # logs with zero would_reject / rejected / no_expected_sa, and
      # internal.auth.ok on all seven endpoints — including tasks/run, the
      # Cloud Tasks path, which was the last one still unobserved.
      #
      # To roll back, set this to "audit" and redeploy. Enforcing wrongly 401s
      # every scheduled job at once, so the audit verdict is what earns the
      # promotion.
      env {
        name  = "INTERNAL_JOBS_AUTH_MODE"
        value = "enforce"
      }

      # ---- Shopify ----
      env {
        name  = "SHOPIFY_SHOP_DOMAIN"
        value = var.shopify_shop_domain
      }
      env {
        name  = "SHOPIFY_API_VERSION"
        value = "2025-04"
      }
      env {
        name = "SHOPIFY_ACCESS_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["shopify-access-token"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SHOPIFY_WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["shopify-webhook-secret"].secret_id
            version = "latest"
          }
        }
      }

      # ---- Rakuten ----
      env {
        name  = "RAKUTEN_SHOP_URL"
        value = var.rakuten_shop_url
      }
      env {
        name = "RAKUTEN_SERVICE_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["rakuten-service-secret"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "RAKUTEN_LICENSE_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.app_secrets["rakuten-license-key"].secret_id
            version = "latest"
          }
        }
      }

      # ---- Admin UI Basic Auth ----
      env {
        name  = "ADMIN_USERNAME"
        value = var.admin_username
      }
      env {
        name  = "ADMIN_PASSWORD"
        value = var.admin_password
      }

      # This block was absent, so the service silently ran on Cloud Run v2's
      # 512Mi default — not a sizing decision anyone made. The BigQuery export
      # runs in this same container and was OOM-killed on 2026-08-21 while
      # catching up an 82-day backlog. The export is now windowed per day
      # (app/cli/export_to_bq.py), which is the actual fix; 2Gi is the headroom
      # that stops a single large day from being fatal.
      resources {
        limits = {
          memory = "2Gi"
          cpu    = "1"
        }
        cpu_idle = true
      }

      volume_mounts {
        name       = "cloudsql"
        mount_path = "/cloudsql"
      }
    }

    volumes {
      name = "cloudsql"
      cloud_sql_instance {
        instances = [google_sql_database_instance.main.connection_name]
      }
    }
  }

  depends_on = [google_project_service.required]
}

# NOTE: Cloud Run service IAM bindings are performed by a project OWNER
# (the client) manually. After this initial deployment, run:
#
#   # Allow scheduler/internal SA to invoke the service
#   gcloud run services add-iam-policy-binding product-system \
#       --project=inventory-496204 --region=asia-northeast1 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/run.invoker
#
#   # Allow Shopify webhooks (unauthenticated) to hit the service
#   gcloud run services add-iam-policy-binding product-system \
#       --project=inventory-496204 --region=asia-northeast1 \
#       --member=allUsers \
#       --role=roles/run.invoker
#
# (The dev account's Editor role lacks run.services.setIamPolicy.)

# NOTE: Project-level IAM binding for roles/cloudsql.client is performed
# by a project OWNER (the client) manually via:
#
#   gcloud projects add-iam-policy-binding inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/cloudsql.client
#
# The developer's Editor role lacks resourcemanager.projects.setIamPolicy.
