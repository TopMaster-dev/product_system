terraform {
  required_version = ">= 1.6.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Configure remote state in GCS for collaborative workflows.
  # backend "gcs" {
  #   bucket = "REPLACE-WITH-PROJECT-tfstate"
  #   prefix = "product-system"
  # }
}
