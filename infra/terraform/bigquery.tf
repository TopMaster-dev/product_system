# BigQuery — dataset already exists in the project (created by client).
# We reference it via a data source and only manage the 6 tables + IAM.

data "google_bigquery_dataset" "main" {
  dataset_id = var.bigquery_dataset
}

resource "google_bigquery_table" "master_skus" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "master_skus"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/master_skus.json")
}

resource "google_bigquery_table" "channel_sku_mappings" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "channel_sku_mappings"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/channel_sku_mappings.json")
}

resource "google_bigquery_table" "orders" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "orders"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/orders.json")

  time_partitioning {
    type  = "DAY"
    field = "ordered_at"
  }
}

resource "google_bigquery_table" "order_items" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "order_items"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/order_items.json")
}

resource "google_bigquery_table" "inventory_events" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "inventory_events"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/inventory_events.json")

  time_partitioning {
    type  = "DAY"
    field = "occurred_at"
  }
}

resource "google_bigquery_table" "inventory_snapshots" {
  dataset_id          = data.google_bigquery_dataset.main.dataset_id
  table_id            = "inventory_snapshots"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/inventory_snapshots.json")
}

# NOTE: BigQuery needs TWO grants, both performed by the project/dataset OWNER
# (the client) — the developer account cannot set IAM. BOTH are required; the
# second was missing until 2026-08-19, so the daily export had never succeeded
# (every run failed with "does not have bigquery.jobs.create permission", and the
# job endpoint used to swallow that as HTTP 200). See docs/25.
#
# 1) dataset-level: permission to WRITE rows
#   bq add-iam-policy-binding \
#       --project_id=inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/bigquery.dataEditor \
#       product_system
#
# 2) project-level: permission to CREATE the load job (NOT implied by dataEditor)
#   gcloud projects add-iam-policy-binding inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/bigquery.jobUser
