# PayGuard infrastructure (Terraform)

**Status: written, not run.** There is no Google Cloud project, billing
account, or `terraform` CLI available in the environment this was authored
in — nothing here has been `terraform validate`d, `plan`ned, or `apply`d.
All ten `.tf` files parse as syntactically valid HCL (checked with the
`python-hcl2` library, which confirms brace/block structure only — it does
NOT check that resource arguments match the actual `google` provider schema,
that referenced attributes exist, or that anything here would actually
apply). Treat it as a reviewed starting point, not verified infrastructure.
Before using it:

1. Read every file. This provisions billable resources (Cloud Run, KMS,
   Firestore) once applied against a real project.
2. Run `terraform init && terraform validate` and fix whatever that
   surfaces — it has not been done here.
3. Fill in `terraform.tfvars` (see `variables.tf`) with a real
   `project_id`, `region` (must support Cloud Run, Firestore, KMS *and*
   the pinned Gemini model — SPEC.md Section 14), and `billing_account_id`.
4. Review `iam.tf` particularly closely: it is the file that encodes
   SPEC.md Section 4's trust boundaries (only the authority and receipts
   service accounts get KMS signer permission; the runner gets neither
   Firestore nor KMS access; internal Cloud Run calls require the caller's
   own service account as the verified principal, never a shared secret).

## What this provisions

| File | Contents |
|---|---|
| `service_accounts.tf` | One service account per SPEC.md Section 4 service: authority, gateway, runner, receipts |
| `kms.tf` | Two asymmetric-signing key rings: `authority-key` (mandates + grants) and `receipt-key` (receipts), RSA-PSS-2048-SHA256 |
| `iam.tf` | KMS signer role granted ONLY to authority (its own key) and receipts (its own key); no other service account gets any KMS role. Cloud Run invoker bindings scoped per internal caller, not "allUsers" |
| `firestore.tf` | Native-mode Firestore database |
| `cloud_run.tf` | Four Cloud Run services (authority public-ish behind auth, gateway/runner/receipts internal-ingress-only), each with its own service account and minimal env vars — no service embeds another's credentials |
| `scheduler.tf` | Two Cloud Scheduler jobs (authority signature drain, receipt drain) invoking their internal endpoints with OIDC tokens, matching SPEC.md Section 12's scheduler-only internal endpoints |
| `budget.tf` | A billing budget alert — SPEC.md Section 14: "Billing alerts alone do not stop charges," so `cloud_run.tf` also sets `max_instance_count` per service |
| `variables.tf` / `outputs.tf` | Inputs (project, region, pinned model id, image tags) and outputs (service URLs, key resource names) |

## What this deliberately does NOT do

- Build or push container images — `cloud_run.tf` references image tags
  you supply; CI builds them (SPEC.md Section 14 step 3).
- Configure Firebase Auth or Firebase Hosting — those are console/Firebase
  CLI steps, not Terraform-managed in this scaffold.
- Seed Firestore with demo data — that is `POST /internal/demo/scenarios/seed`
  at runtime, not infrastructure.
- Grant the runner service account *any* Firestore or KMS role. If a future
  edit adds one, that is a regression against SPEC.md Section 4 and should
  be caught in review, not silently accepted because "it was easier."
