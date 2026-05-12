resource "google_sql_database_instance" "main" {
  name             = "product-system"
  database_version = "POSTGRES_15"
  region           = var.region
  deletion_protection = true

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    backup_configuration {
      enabled    = true
      start_time = "18:00" # 03:00 JST
    }
    ip_configuration {
      ipv4_enabled = false
      private_network = google_compute_network.default.id
    }
  }

  depends_on = [google_project_service.required]
}

resource "google_sql_database" "app" {
  name     = "product_system"
  instance = google_sql_database_instance.main.name
}

resource "google_sql_user" "app" {
  name     = "postgres"
  instance = google_sql_database_instance.main.name
  password = var.db_password
}

# Placeholder for private VPC for Private IP Cloud SQL.
resource "google_compute_network" "default" {
  name                    = "product-system-vpc"
  auto_create_subnetworks = true
}
