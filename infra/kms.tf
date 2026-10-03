# Two asymmetric-signing keys (SPEC.md Section 5: "Mandates and grants
# share one authority KMS key in the MVP... Receipts use a second KMS
# key"). RSA-PSS-2048-SHA256 matches the PS256 JWS profile in
# services/shared/jws.py.

resource "google_kms_key_ring" "payguard" {
  name     = "payguard-keys"
  location = var.region
}

resource "google_kms_crypto_key" "authority_key" {
  name            = "authority-key"
  key_ring        = google_kms_key_ring.payguard.id
  purpose         = "ASYMMETRIC_SIGN"
  rotation_period = null # explicit: manual rotation only, matching the pinned-kid verification profile

  version_template {
    algorithm        = "RSA_SIGN_PSS_2048_SHA256"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_kms_crypto_key" "receipt_key" {
  name            = "receipt-key"
  key_ring        = google_kms_key_ring.payguard.id
  purpose         = "ASYMMETRIC_SIGN"
  rotation_period = null

  version_template {
    algorithm        = "RSA_SIGN_PSS_2048_SHA256"
    protection_level = "SOFTWARE"
  }

  lifecycle {
    prevent_destroy = true
  }
}
