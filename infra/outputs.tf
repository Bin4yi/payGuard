output "authority_url" {
  value = google_cloud_run_v2_service.authority.uri
}

output "gateway_url" {
  value = google_cloud_run_v2_service.gateway.uri
}

output "runner_url" {
  value = google_cloud_run_v2_service.runner.uri
}

output "receipts_url" {
  value = google_cloud_run_v2_service.receipts.uri
}

output "authority_kms_key" {
  value = google_kms_crypto_key.authority_key.id
}

output "receipt_kms_key" {
  value = google_kms_crypto_key.receipt_key.id
}
