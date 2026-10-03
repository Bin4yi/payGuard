resource "google_firestore_database" "payguard" {
  project     = var.project_id
  name        = "(default)"
  location_id = var.region
  type        = "FIRESTORE_NATIVE"

  # SPEC.md Section 4: "Firestore server SDK access uses IAM and is not
  # protected by client Security Rules." No google_firestore_document or
  # security-rules resource is declared here on purpose — ownership and
  # operation checks are enforced in server code (services/gateway,
  # services/authority), not in Firestore rules.
}
