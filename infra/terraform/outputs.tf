output "service_url" {
  value       = google_cloud_run_v2_service.app.uri
  description = "Public Cloud Run service URL."
}

output "service_account_email" {
  value       = google_service_account.app.email
  description = "Service account used by the Cloud Run service + scheduler invocations."
}

output "cloudsql_connection_name" {
  value       = google_sql_database_instance.main.connection_name
  description = "Use this in the DATABASE_URL host=/cloudsql/... segment."
}

output "tasks_queue" {
  value = google_cloud_tasks_queue.webhook.name
}
