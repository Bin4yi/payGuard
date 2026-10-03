# This file encodes SPEC.md Section 4's trust boundaries in IAM. Read it
# alongside services/shared/local_adapters.py's comment: the runner must
# NEVER be able to sign or write Firestore, and every other service must
# get only the KMS key it actually owns — never the other one.

# --- KMS: only authority signs mandates/grants; only receipts signs receipts. ---

resource "google_kms_crypto_key_iam_member" "authority_signs_authority_key" {
  crypto_key_id = google_kms_crypto_key.authority_key.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.authority.email}"
}

resource "google_kms_crypto_key_iam_member" "receipts_signs_receipt_key" {
  crypto_key_id = google_kms_crypto_key.receipt_key.id
  role          = "roles/cloudkms.signerVerifier"
  member        = "serviceAccount:${google_service_account.receipts.email}"
}

# Deliberately absent: any IAM binding granting the gateway or runner
# service accounts a role on either KMS key. If a future change adds one,
# that is a regression — SPEC.md Section 4: "Runner ... May NOT do: ...
# access any KMS key"; "Gateway ... May NOT do: Sign receipts."

# --- Firestore: native-mode access uses IAM, not client Security Rules
# (SPEC.md Section 4). The runner gets no Firestore role at all. ---

resource "google_project_iam_member" "authority_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.authority.email}"
}

resource "google_project_iam_member" "gateway_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "receipts_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.receipts.email}"
}

# --- Cloud Run invoker bindings: internal callers only, never allUsers,
# and each binding names the SPECIFIC caller allowed (SPEC.md Section 12
# "Internal endpoints require a verified service principal plus job or
# grant authorisation"). ---

resource "google_cloud_run_v2_service_iam_member" "gateway_invoked_by_runner" {
  name     = google_cloud_run_v2_service.gateway.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_cloud_run_v2_service_iam_member" "runner_context_invoked_by_authority" {
  # The runner's context/grant lookups are pulled by the runner itself in
  # this design (SPEC.md Section 6: "The private grant endpoint returns
  # only the persisted token to its assigned job/runner"), so the runner
  # must be allowed to invoke the authority's internal endpoints.
  name     = google_cloud_run_v2_service.authority.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_cloud_run_v2_service_iam_member" "authority_invoked_by_scheduler" {
  name     = google_cloud_run_v2_service.authority.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_run_v2_service_iam_member" "receipts_invoked_by_scheduler" {
  name     = google_cloud_run_v2_service.receipts.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# The authority service is the only one a browser calls directly; its own
# invoker binding for end users is handled by whatever auth proxy / Firebase
# Hosting rewrite fronts it, not modeled here (out of Terraform's scope —
# see infra/README.md "What this deliberately does NOT do").
