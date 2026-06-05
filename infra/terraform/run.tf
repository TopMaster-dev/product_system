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
