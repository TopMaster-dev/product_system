resource "google_sql_database_instance" "main" {
  name                = "product-system"
  database_version    = "POSTGRES_15"
  region              = var.region
  deletion_protection = true

  settings {
    tier              = var.db_tier
    availability_type = "ZONAL"
    backup_configuration {
      enabled    = true
      start_time = "18:00" # 03:00 JST
    }
    # Phase 1-A: Public IP with no authorized_networks; Cloud Run reaches the
    # instance via the authenticated Cloud SQL Auth Proxy socket. Direct
    # external connections are rejected because no authorized_networks
    # entries are declared. Revisit (private IP + VPC peering) in Phase 1-B.
    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
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
