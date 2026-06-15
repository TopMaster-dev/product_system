resource "google_cloud_tasks_queue" "webhook" {
  name     = "product-system-webhook"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 50
  }

  retry_config {
    max_attempts  = 7
    min_backoff   = "1s"
    max_backoff   = "300s"
    max_doublings = 6
  }

  depends_on = [google_project_service.required]
}
