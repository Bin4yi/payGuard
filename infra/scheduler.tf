# SPEC.md Section 12: POST /internal/authority/drain and
# POST /internal/receipts/drain — "Recover pending signatures" and
# "Process bounded pending events" respectively. Both are scheduler-only,
# never publicly reachable (see cloud_run.tf's INGRESS_TRAFFIC_INTERNAL_ONLY
# and iam.tf's invoker bindings for the scheduler service account).

resource "google_cloud_scheduler_job" "authority_drain" {
  name      = "payguard-authority-drain"
  region    = var.region
  schedule  = "*/2 * * * *" # every 2 minutes; tune after observing real signing latency
  time_zone = "UTC"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.authority.uri}/v1/internal/authority/drain"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience               = google_cloud_run_v2_service.authority.uri
    }
  }

  retry_config {
    retry_count = 3
  }
}

resource "google_cloud_scheduler_job" "receipts_drain" {
  name      = "payguard-receipts-drain"
  region    = var.region
  schedule  = "*/1 * * * *" # every minute; keeps the receipt chain close to real-time
  time_zone = "UTC"

  http_target {
    http_method = "POST"
    uri         = "${google_cloud_run_v2_service.receipts.uri}/v1/internal/receipts/drain"

    oidc_token {
      service_account_email = google_service_account.scheduler.email
      audience               = google_cloud_run_v2_service.receipts.uri
    }
  }

  retry_config {
    retry_count = 3
  }
}
