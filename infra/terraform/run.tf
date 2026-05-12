resource "google_service_account" "app" {
  account_id   = "product-system-app"
  display_name = "Product System service account"
}

resource "google_cloud_run_v2_service" "app" {
  name     = "product-system"
  location = var.region

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

# Bind invoker permission for the scheduler / Cloud Tasks service accounts.
resource "google_cloud_run_v2_service_iam_member" "self_invoker" {
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.app.email}"
}
