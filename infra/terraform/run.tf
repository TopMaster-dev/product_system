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

      env {
        name  = "APP_ENV"
        value = "prod"
      }
      env {
        name  = "DATABASE_URL"
        value = "postgresql+asyncpg://postgres:${var.db_password}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
      }
      env {
        name  = "DATABASE_URL_SYNC"
        value = "postgresql+psycopg2://postgres:${var.db_password}@/${google_sql_database.app.name}?host=/cloudsql/${google_sql_database_instance.main.connection_name}"
      }
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

# NOTE: Cloud Run service IAM binding is performed by a project OWNER
# (the client) manually via:
#
#   gcloud run services add-iam-policy-binding product-system \
#       --project=inventory-496204 \
#       --region=asia-northeast1 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/run.invoker
#
# This grants the app SA permission to invoke its own Cloud Run service —
# required so Cloud Scheduler can trigger the polling / BQ export jobs.

# NOTE: Project-level IAM binding for roles/cloudsql.client is performed
# by a project OWNER (the client) manually via:
#
#   gcloud projects add-iam-policy-binding inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/cloudsql.client
#
# The developer's Editor role lacks resourcemanager.projects.setIamPolicy.
