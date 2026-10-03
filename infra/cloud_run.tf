# Four separate Cloud Run services, one per SPEC.md Section 4 trust
# boundary — deliberately NOT the single-process demo app in services/api/;
# splitting that monolith into services that only call each other over
# verified HTTP (no shared Python import of another service's signer or
# store) is unfinished application work, tracked in README.md's Phase 5
# section, not something Terraform can paper over.

locals {
  common_env = [
    { name = "PAYGUARD_PROJECT_ID", value = var.project_id },
    { name = "PAYGUARD_REGION", value = var.region },
    { name = "PAYGUARD_POLICY_VERSION", value = var.policy_version },
  ]
}

resource "google_cloud_run_v2_service" "authority" {
  name     = "payguard-authority"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL" # the only service a browser calls; auth is enforced in app code

  template {
    service_account = google_service_account.authority.email
    max_instance_request_concurrency = 20

    scaling {
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.container_images.authority

      dynamic "env" {
        for_each = concat(local.common_env, [
          { name = "PAYGUARD_AUTHORITY_KEY_RESOURCE", value = google_kms_crypto_key.authority_key.id },
        ])
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "gateway" {
  name     = "payguard-gateway"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.gateway.email

    scaling {
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.container_images.gateway

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "runner" {
  name     = "payguard-runner"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.runner.email

    scaling {
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.container_images.runner

      dynamic "env" {
        for_each = concat(local.common_env, [
          { name = "PAYGUARD_PINNED_MODEL_ID", value = var.pinned_model_id },
          { name = "PAYGUARD_GATEWAY_URL", value = google_cloud_run_v2_service.gateway.uri },
          { name = "PAYGUARD_AUTHORITY_URL", value = google_cloud_run_v2_service.authority.uri },
        ])
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
    }
  }
}

resource "google_cloud_run_v2_service" "receipts" {
  name     = "payguard-receipts"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.receipts.email

    scaling {
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.container_images.receipts

      dynamic "env" {
        for_each = concat(local.common_env, [
          { name = "PAYGUARD_RECEIPT_KEY_RESOURCE", value = google_kms_crypto_key.receipt_key.id },
        ])
        content {
          name  = env.value.name
          value = env.value.value
        }
      }
    }
  }
}
