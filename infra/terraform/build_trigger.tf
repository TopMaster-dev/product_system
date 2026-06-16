# Cloud Build trigger — build + push the image on every push to main.
#
# Runs cloudbuild.yaml with _TAG=$SHORT_SHA, so each main commit yields an
# immutable, SHA-tagged image (plus :latest). Build/push ONLY: it never
# deploys — promotion to the Cloud Run service / jobs stays a deliberate
# manual or terraform step.
#
# Gated behind create_build_trigger (default false).
#
# HARD PREREQUISITE: the GitHub repo must already be connected to Cloud Build
# via the Cloud Build GitHub App (one-time, console: Cloud Build > Triggers >
# Connect Repository > GitHub). That step needs GitHub repo-admin and cannot
# be expressed in Terraform; `terraform apply` of this trigger FAILS until the
# repo is connected. See docs/14 §10 for the dev-vs-owner breakdown.

resource "google_cloudbuild_trigger" "build_on_main" {
  count = var.create_build_trigger ? 1 : 0

  name        = "product-system-build-main"
  description = "Build + push the app image on push to main (cloudbuild.yaml)."

  github {
    owner = var.github_owner
    name  = var.github_repo
    push {
      branch = "^main$"
    }
  }

  filename = "cloudbuild.yaml"

  # The trigger value overrides cloudbuild.yaml's _TAG default ("latest").
  # $SHORT_SHA is a built-in populated only for commit-triggered builds.
  substitutions = {
    _TAG = "$SHORT_SHA"
  }

  depends_on = [google_project_service.required]
}
