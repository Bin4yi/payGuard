variable "project_id" {
  description = "Dedicated demo GCP project id (SPEC.md Section 14: least-privilege identities, agreed spend ceiling)."
  type        = string
}

variable "region" {
  description = "Region supported by Cloud Run, Firestore, Cloud KMS AND the pinned Gemini model (SPEC.md Section 14)."
  type        = string
  default     = "us-central1"
}

variable "billing_account_id" {
  description = "Billing account id, for the budget alert in budget.tf."
  type        = string
}

variable "spend_ceiling_usd" {
  description = "Planning allowance for the demo project (PROJECT.md Section 9: USD 100)."
  type        = number
  default     = 100
}

variable "pinned_model_id" {
  description = "Exact pinned Gemini model id. Never 'latest' or an unverified preview name (SPEC.md Section 10)."
  type        = string

  validation {
    condition     = var.pinned_model_id != "latest" && var.pinned_model_id != ""
    error_message = "pinned_model_id must be an exact model identifier, never 'latest' or empty."
  }
}

variable "policy_version" {
  description = "Grant/mandate policy_version (SPEC.md Section 6/7)."
  type        = string
  default     = "rent-v1"
}

variable "container_images" {
  description = "Fully-qualified container image references per service, built by CI (SPEC.md Section 14 step 3)."
  type = object({
    authority = string
    gateway   = string
    runner    = string
    receipts  = string
  })
}

variable "max_instance_count" {
  description = "Per-service Cloud Run max instances — a billing alert alone does not stop charges (SPEC.md Section 14)."
  type        = number
  default     = 3
}
