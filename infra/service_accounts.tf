# One service account per SPEC.md Section 4 service. No downloadable
# long-lived keys are created for any of these — Cloud Run assigns them
# automatically and internal calls use Google-issued ID tokens.

resource "google_service_account" "authority" {
  account_id   = "payguard-authority"
  display_name = "PayGuard authority service"
}

resource "google_service_account" "gateway" {
  account_id   = "payguard-gateway"
  display_name = "PayGuard payment gateway service"
}

resource "google_service_account" "runner" {
  account_id   = "payguard-runner"
  display_name = "PayGuard Gemini runner service (no Firestore, no KMS)"
}

resource "google_service_account" "receipts" {
  account_id   = "payguard-receipts"
  display_name = "PayGuard receipt signing service"
}

resource "google_service_account" "scheduler" {
  account_id   = "payguard-scheduler-invoker"
  display_name = "Identity Cloud Scheduler uses to call internal drain endpoints"
}
