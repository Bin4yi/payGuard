# SPEC.md Section 14: "Billing alerts alone do not stop charges. Use max
# instance counts, bounded model calls and per-user quotas." This budget
# alert is a notification, not a spend cap — the actual cap is
# var.max_instance_count on every Cloud Run service plus the application's
# own per-session/aggregate demo quotas (services/authority/demo.py's
# MAX_SCENARIOS_PER_OWNER).

resource "google_billing_budget" "demo_project" {
  billing_account = var.billing_account_id
  display_name    = "payguard-demo-spend-ceiling"

  budget_filter {
    projects = ["projects/${var.project_id}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(var.spend_ceiling_usd)
    }
  }

  threshold_rules {
    threshold_percent = 0.5
  }
  threshold_rules {
    threshold_percent = 0.9
  }
  threshold_rules {
    threshold_percent = 1.0
  }
}
