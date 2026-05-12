# Terraform — Phase 1-A skeleton

Provisions the GCP resources backing this stack:

| File | Resource |
|---|---|
| `apis.tf` | Project-level API enablement |
| `sql.tf` | Cloud SQL (Postgres 15) instance + DB |
| `run.tf` | Cloud Run service + service account |
| `tasks.tf` | Cloud Tasks queue for webhook async processing |
| `scheduler.tf` | Cloud Scheduler jobs (BQ export daily, polling) |
| `secret_manager.tf` | Secret slots for Rakuten + Shopify credentials |
| `bigquery.tf` | Dataset + 6 tables matching the source schema |

## Usage

```sh
cd infra/terraform
terraform init
terraform plan -var project_id=YOUR_PROJECT -var db_password=$(openssl rand -base64 24) \
              -var service_image=gcr.io/YOUR_PROJECT/product-system:latest \
              -var bigquery_dataset=product_system
terraform apply
```

Then populate the secrets out of band:

```sh
echo -n "..." | gcloud secrets versions add rakuten-service-secret --data-file=-
echo -n "..." | gcloud secrets versions add rakuten-license-key --data-file=-
echo -n "..." | gcloud secrets versions add shopify-access-token --data-file=-
echo -n "..." | gcloud secrets versions add shopify-webhook-secret --data-file=-
```

## State

Configure remote state in `versions.tf` (GCS backend block) before
collaborating. The current configuration uses local state and is intended
for the initial bootstrap on a single workstation.
