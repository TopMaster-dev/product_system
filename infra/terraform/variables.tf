variable "project_id" {
  description = "GCP project that hosts this stack."
  type        = string
}

variable "region" {
  description = "Default region for Cloud SQL / Run / Tasks / Scheduler."
  type        = string
  default     = "asia-northeast1"
}

variable "service_image" {
  description = "Fully qualified container image for the main service."
  type        = string
}

variable "bigquery_dataset" {
  description = "Existing BigQuery dataset where daily exports land."
  type        = string
}

variable "db_tier" {
  description = "Cloud SQL machine type."
  type        = string
  default     = "db-f1-micro"
}

variable "db_password" {
  description = "Bootstrap password for the postgres user (rotate after first deploy)."
  type        = string
  sensitive   = true
}
