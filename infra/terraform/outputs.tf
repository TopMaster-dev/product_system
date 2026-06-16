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

output "verification_job_names" {
  value       = sort([for j in google_cloud_run_v2_job.verification : j.name])
  description = "Names of the deployed verification Cloud Run Jobs (empty unless create_verification_jobs = true)."
}

output "recon_verify_bucket" {
  value       = var.create_verification_jobs ? google_storage_bucket.recon_verify[0].name : ""
  description = "GCS bucket for F1.8 reconcile dummy CSVs (empty unless create_verification_jobs = true)."
}

output "image_registry_base" {
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo}"
  description = "Base path for the container image; append /app:<tag>. Matches cloudbuild.yaml output."
}
