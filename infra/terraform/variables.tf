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
  description = "Fully qualified container image for the main service. Default is the public hello placeholder so the initial terraform apply can finish before we have a real image to deploy."
  type        = string
  default     = "us-docker.pkg.dev/cloudrun/container/hello"
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

variable "shopify_shop_domain" {
  description = "Shopify admin domain (e.g. xxxxx.myshopify.com)."
  type        = string
}

variable "rakuten_shop_url" {
  description = "Rakuten storefront URL (e.g. https://www.rakuten.co.jp/yourshop/)."
  type        = string
}

variable "admin_username" {
  description = "Basic Auth username for the admin UI."
  type        = string
  default     = "admin"
}

variable "admin_password" {
  description = "Basic Auth password for the admin UI."
  type        = string
  sensitive   = true
}
