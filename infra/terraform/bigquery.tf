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

# NOTE: BigQuery Dataset IAM binding is performed by the dataset OWNER
# (the client) manually via:
#
#   bq add-iam-policy-binding \
#       --project_id=inventory-496204 \
#       --member=serviceAccount:product-system-app@inventory-496204.iam.gserviceaccount.com \
#       --role=roles/bigquery.dataEditor \
#       product_system
#
# This avoids needing dataset-owner permissions on the developer account.
