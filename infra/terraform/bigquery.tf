# BigQuery dataset + table skeletons.
# Schemas are kept inline as JSON files for clarity; partitioned by date.

resource "google_bigquery_dataset" "main" {
  dataset_id = var.bigquery_dataset
  location   = "asia-northeast1"

  depends_on = [google_project_service.required]
}

resource "google_bigquery_table" "master_skus" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "master_skus"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/master_skus.json")
}

resource "google_bigquery_table" "channel_sku_mappings" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "channel_sku_mappings"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/channel_sku_mappings.json")
}

resource "google_bigquery_table" "orders" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "orders"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/orders.json")

  time_partitioning {
    type  = "DAY"
    field = "ordered_at"
  }
}

resource "google_bigquery_table" "order_items" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "order_items"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/order_items.json")
}

resource "google_bigquery_table" "inventory_events" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "inventory_events"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/inventory_events.json")

  time_partitioning {
    type  = "DAY"
    field = "occurred_at"
  }
}

resource "google_bigquery_table" "inventory_snapshots" {
  dataset_id          = google_bigquery_dataset.main.dataset_id
  table_id            = "inventory_snapshots"
  deletion_protection = false
  schema              = file("${path.module}/bq_schemas/inventory_snapshots.json")
}

# Allow the app to write into the dataset.
resource "google_bigquery_dataset_iam_member" "app_writer" {
  dataset_id = google_bigquery_dataset.main.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.app.email}"
}
