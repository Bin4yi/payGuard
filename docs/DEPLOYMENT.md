# PayGuard deployment plan and guide

This document covers two things:

1. **Stage 0: a public demo you can deploy today.** One container on Google
   Cloud Run serves both the web app and the API. It runs the same code as
   the local demo, and its limits are listed below.
2. **Stage 1: the production-shaped deployment** described in SPEC.md
   (four services, Firestore, Cloud KMS, Firebase Auth, Gemini). It is a
   plan with work items and acceptance criteria, not something you can
   run yet.

Everything is synthetic. The simulated ledger never touches real money,
and no stage of this plan connects to a bank.

---

## 0. Verified vs. not verified

| Item | Status |
|---|---|
| `Dockerfile` builds (web build + Python runtime, 296 MB image) | **Verified locally** (Docker Desktop 29.5.3) |
| Container serves web app + API on one origin, `/healthz` OK | **Verified locally** |
| Full asserted Playwright story passes against the container (`record_demo.py --url`) | **Verified locally** |
| 198 automated tests pass | **Verified locally** |
| `gcloud run deploy` of this image | **Not run.** No Google Cloud project or `gcloud` was available when this was written |
| `infra/` Terraform | **Not run.** HCL syntax only; never `validate`d, `plan`ned or `apply`d |
| Stage 1 adapters (Firestore, KMS, Firebase, Gemini, OIDC) | **Not implemented.** Only interfaces and stubs exist |

The Cloud Run commands below follow Google's documented CLI. Because they
have not been run against a real project, check them against the
`gcloud` version you install.

---

## 1. Why Stage 0 must run as exactly one instance

Read this before you touch any scaling setting.

The demo backend (`services/api/main.py` → `build_local_demo_state()`)
keeps **all state in the memory of one Python process**. That includes
scenarios, mandates, grants, the "paid at most once" obligation lock, the
ledger, receipts and signing keys. As a result:

- **More than one instance breaks correctness.** Two instances would have
  two separate ledgers and two separate obligation locks. A browser whose
  requests were load-balanced across them would see mandates disappear,
  and the "exactly once" guarantee would hold only within each instance.
  So set `--max-instances 1`, and run uvicorn with `--workers 1` (the
  Dockerfile already does).
- **A restart wipes everything.** A redeploy, a crash, or Cloud Run
  scaling the service to zero all start from empty state with new signing
  keys. Earlier receipts can then no longer be verified by the new
  process. `--min-instances 1` stops scale-to-zero, but a redeploy still
  resets everything. For a demo that is acceptable, since each visitor
  gets a fresh isolated scenario.
- **During a redeploy two revisions overlap briefly.** Someone clicking
  through the demo at that moment can land on the new, empty revision. To
  avoid it, redeploy when nobody is presenting.

Stage 1 fixes all three by moving state into Firestore and keys into Cloud
KMS (see §6).

---

## 2. What was changed to make Stage 0 deployable

| Change | Why |
|---|---|
| `Dockerfile` + `.dockerignore` (repo root) | Multi-stage build: Node builds `web/dist` with an empty API base URL (same origin), then a slim Python image runs uvicorn as a non-root user on `$PORT` (Cloud Run sets `PORT=8080`). |
| Same-origin static serving (`PAYGUARD_WEB_DIST`) | One URL serves the page and the API, so no cross-origin setup is needed. Unknown `/v1/...` paths still return 404 instead of the web page. Path traversal is blocked. |
| CORS allowlist (`PAYGUARD_CORS_ORIGINS`) | Replaces `allow_origins=["*"]`. The default allows only the local Vite dev/preview ports, and `*` is refused at startup. |
| HMAC-signed demo session tokens (`PAYGUARD_SESSION_SECRET`) | Before this, a session token was the plain string `local:<uid>:...`, so anyone who learned a uid could act as that user. Tokens now carry an HMAC-SHA256 signature over a server secret (`services/shared/identity.py`, tests in `tests/security/test_session_tokens.py`). |
| `GET /healthz` | Health check for Cloud Run and the smoke test. |
| `record_demo.py --url <deployed URL>` | Runs the asserted browser story against a live deployment. This is the post-deploy smoke test. |

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port (Cloud Run sets it). |
| `PAYGUARD_WEB_DIST` | `/app/web/dist` in the image | Built web app to serve at `/`. Leave it unset locally when using Vite. |
| `PAYGUARD_SCREENER` | `laya-hf` | `laya-hf` (free public Hugging Face Space), `laya-local` (needs the `laya-local` extra; not in the image), or `none`. |
| `PAYGUARD_SESSION_SECRET` | random per process | At least 32 characters. Optional in Stage 0 (state is per-process anyway). Set it from Secret Manager so it isn't generated fresh on each start. |
| `PAYGUARD_CORS_ORIGINS` | local dev ports | Comma-separated origins. Only needed if the web app is hosted on a **different** origin from the API. |

---

## 3. Stage 0: deploy the demo to Cloud Run

### 3.1 Prerequisites (one-time)

1. A Google Cloud project with **billing enabled**. Cloud Run needs billing
   even when usage stays inside the free tier.
2. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install),
   then sign in:
   ```bash
   gcloud auth login
   gcloud config set project YOUR_PROJECT_ID
   gcloud config set run/region asia-south1   # Mumbai; pick the region nearest your users
   ```
3. Enable the APIs this stage uses:
   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com \
       artifactregistry.googleapis.com secretmanager.googleapis.com
   ```
4. Optional: install Docker Desktop to build and test the image locally
   first (§3.2). It is not required, because `--source .` builds in Cloud
   Build.

### 3.2 Test the exact image locally

Test locally before spending any cloud time.

```bash
docker build -t payguard-demo:local .
docker run --rm -p 8080:8080 payguard-demo:local
# second terminal:
curl http://127.0.0.1:8080/healthz                       # {"status":"ok"}
.venv/Scripts/python scripts/demo/record_demo.py --url http://127.0.0.1:8080 --pace 0.2
```

The last command drives a real browser through the whole story: pay,
receipts, replay, poisoned notice, account change. It asserts every
outcome and exits non-zero if anything is wrong.

### 3.3 Create a dedicated runtime identity and the session secret

Do not run the service as the default compute service account, which
usually has broad project Editor rights. This identity needs one
permission: reading one secret.

```bash
gcloud iam service-accounts create payguard-demo --display-name "PayGuard demo runtime"

openssl rand -hex 32 | tr -d '\n' | gcloud secrets create payguard-session-secret --data-file=-

gcloud secrets add-iam-policy-binding payguard-session-secret \
    --member "serviceAccount:payguard-demo@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
    --role roles/secretmanager.secretAccessor
```

### 3.4 Deploy

```bash
gcloud run deploy payguard-demo \
    --source . \
    --service-account payguard-demo@YOUR_PROJECT_ID.iam.gserviceaccount.com \
    --allow-unauthenticated \
    --min-instances 1 \
    --max-instances 1 \
    --concurrency 40 \
    --cpu 1 --memory 512Mi \
    --timeout 60 \
    --set-env-vars PAYGUARD_SCREENER=laya-hf \
    --set-secrets PAYGUARD_SESSION_SECRET=payguard-session-secret:latest
```

What each flag does:

- `--source .` has Cloud Build build the repo's `Dockerfile` and store the
  image in an Artifact Registry repository. The CLI offers to create
  `cloud-run-source-deploy` the first time.
- `--allow-unauthenticated`: the demo is public, and each visitor gets an
  isolated synthetic session. PayGuard does its own authentication on
  every request after that.
- `--min-instances 1 --max-instances 1`: the correctness requirement from §1.
- `--concurrency 40`: one instance handles many requests at once, since
  most time is spent waiting on the Laya Space. Keep it modest.

The command prints the service URL, e.g.
`https://payguard-demo-xxxxxxxx.asia-south1.run.app`.

### 3.5 Verify the deployment

```bash
URL=$(gcloud run services describe payguard-demo --format 'value(status.url)')
curl "$URL/healthz"
.venv/Scripts/python scripts/demo/record_demo.py --url "$URL" --pace 0.3
gcloud run services logs read payguard-demo --limit 50
```

Checklist:

- [ ] `/healthz` returns `{"status":"ok"}`
- [ ] The smoke test prints `smoke test passed`
- [ ] The logs show no tracebacks
- [ ] The Cloud Run console shows **1** instance, never 2 (Metrics → Instance count)
- [ ] A hand-made token is rejected:
      `curl -X POST -H "Authorization: Bearer local:anon-x:anonymous:0" "$URL/v1/demo/scenarios"` → `401`
- [ ] Internal endpoints reject user tokens: `POST $URL/v1/internal/receipts/drain` with a session token → `401`

### 3.6 Cost controls

- **Set a budget alert**, and keep in mind that it only notifies you. It
  does not stop charges:
  ```bash
  gcloud billing budgets create --billing-account YOUR_BILLING_ACCOUNT \
      --display-name "payguard-demo" --budget-amount 10USD \
      --threshold-rule percent=0.5 --threshold-rule percent=0.9 --threshold-rule percent=1.0
  ```
- The hard ceiling is `--max-instances 1` with 1 vCPU / 512 MiB. The
  largest recurring cost is `--min-instances 1`, which keeps one instance
  warm around the clock (billed at Cloud Run's lower idle rate). Check the
  current price for your region in the
  [Cloud Run pricing calculator](https://cloud.google.com/run/pricing).
- **Cheaper option:** `--min-instances 0`. Idle costs drop to near zero,
  but the service scales to zero after a quiet period and everyone's demo
  state resets. That is fine if you only open it for presentations.
- Laya runs on a free public Hugging Face Space. It costs nothing, but
  that also means no SLA: it can be slow or asleep. PayGuard fails open
  (screening shows "unavailable"). The deterministic runtime and gateway
  checks still block the poisoned-notice attack, so the demo stays safe.

### 3.7 Update, roll back, tear down

```bash
# update: rerun the deploy command from 3.4 (this resets demo state, see §1)

# roll back to the previous revision
gcloud run revisions list --service payguard-demo
gcloud run services update-traffic payguard-demo --to-revisions PREVIOUS_REVISION=100

# tear down everything Stage 0 created
gcloud run services delete payguard-demo
gcloud secrets delete payguard-session-secret
gcloud iam service-accounts delete payguard-demo@YOUR_PROJECT_ID.iam.gserviceaccount.com
gcloud artifacts repositories delete cloud-run-source-deploy --location asia-south1
```

### 3.8 Optional: custom domain via Firebase Hosting

The `firebase` CLI is already installed on the dev machine. Firebase
Hosting can put a custom domain in front of the Cloud Run service while
keeping everything on one origin. Create `firebase.json`:

```json
{
  "hosting": {
    "public": "web/dist",
    "rewrites": [{ "source": "**", "run": { "serviceId": "payguard-demo", "region": "asia-south1" } }]
  }
}
```

Then run `firebase deploy --only hosting`. Every path forwards to the
container, which already serves the web app. `public` must still point at
an existing folder.

### 3.9 Alternative: Hugging Face Spaces (free, no billing account)

A Docker Space runs one container on free CPU, which fits the
single-instance rule by construction. Create a Space with the **Docker**
SDK, push this repo to it, and add to the Space's `README.md` front matter:

```yaml
sdk: docker
app_port: 8080
```

Tradeoffs: the Space sleeps after inactivity, which resets state; the
repository must be pushed to Hugging Face (this project has not been
pushed anywhere yet); and you get none of the Google Cloud controls
above. Laya calls then go from one Space to another.

---

## 4. Stage 0 limits (say these out loud in a demo)

- Synthetic only. The ledger is simulated, and so are the landlord,
  tenant and bank accounts.
- Signing keys are generated in memory at startup, not held in Cloud KMS,
  and receipts from a previous process can't be verified after a restart.
- Sessions are HMAC-signed demo tokens with no expiry. They are not
  Firebase ID tokens. Anyone can start a fresh anonymous session, and by
  design that session reaches only its own synthetic data.
- The "agent" in Stage 0 is the deterministic `NoticeParserModelClient`,
  not Gemini (§6, item 4). The security argument does not depend on the
  model. The gateway enforces the signed mandate whatever the model
  proposes.
- Screening uses a public, rate-limited, best-effort Space.

---

## 5. Stage 1: production-shaped deployment

This is the SPEC.md architecture: four Cloud Run services, each with its
own service account (`infra/service_accounts.tf`). The trust boundaries
are:

```
browser ──Firebase ID token──▶ authority ──(signs mandates/grants via KMS)
                                   │
          Cloud Scheduler ──OIDC──▶│ drains          runner (untrusted: no Firestore, no KMS)
                                   │                    │ grant only
                               Firestore ◀── gateway ◀──┘  (the only money mover)
                                   ▲
                               receipts ──(signs receipts via KMS)
```

The code already has these seams: interfaces with local test doubles and
real-adapter stubs. Stage 1 means implementing the stubs, and then showing
that the existing test suite still passes against them.

---

## 6. Stage 1 work plan

Do the items in order. Each item finishes with an acceptance check you
can observe.

| # | Work item | Acceptance criteria |
|---|---|---|
| 1 | **Firestore-backed `Store`** with the same transaction contract as the in-memory one: reads before writes, optimistic retry. | The whole suite, including the concurrent double-pay race tests, passes against the Firestore emulator. Two API instances that share one Firestore cannot double-pay an obligation. |
| 2 | **Cloud KMS signer + public-key registry** (`authority-key`, `receipt-key` in `infra/kms.tf`). | Mandate, grant and receipt JWS verify with keys fetched from KMS. Receipts still verify after a restart. No private key material exists outside KMS. |
| 3 | **`FirebaseIdentityVerifier.verify`** via `firebase_admin.auth.verify_id_token`. The web app signs in anonymously with the Firebase JS SDK. **Remove** `POST /v1/demo/anonymous-session` from the production entrypoint. | An expired, forged or wrong-project token → 401. Ownership tests pass unchanged. The production app has no local token issuer. |
| 4 | **`GeminiModelClient`** with a pinned model id (`var.pinned_model_id`) and schema-validated output. | The eval harness (`services/eval/`) runs against Gemini and meets SPEC.md's evaluation gates. The poisoned-notice and redirect cases never produce a payment to an unapproved destination. |
| 5 | **Split into four entrypoints** (authority, gateway, runner, receipts) plus a **Google OIDC `ServicePrincipalVerifier`** in place of `LocalServicePrincipalVerifier`. | Each service rejects every caller that isn't its allowed service account, with the audience checked. The runner container has no Firestore or KMS credentials (negative-access test in CI). |
| 6 | **Terraform:** `terraform init && terraform validate && terraform plan` in `infra/`, fix what it reports, then apply to a **staging** project. | A clean `plan`. `iam.tf` reviewed against SPEC.md §4. Only authority and receipts can sign, each with its own key only. |
| 7 | **CI:** run tests, build four images, push to Artifact Registry, deploy to staging, run `record_demo.py --url <staging>`. | A red test or a failed smoke test blocks promotion to prod. |
| 8 | **Cloud Scheduler drains** (`infra/scheduler.tf`) call `/v1/internal/authority/drain` and `/v1/internal/receipts/drain` with OIDC. | A mandate left PENDING by a signing failure gets signed by the next drain. Receipts are chained with no gaps. |
| 9 | **Frontend on Firebase Hosting** with rewrites to the authority service (same origin), and CORS off or narrowly allowlisted. | No `*` origins anywhere. The browser never sees a grant token (already true today; keep asserting it). |
| 10 | **Operations:** alerts on 5xx rate, gateway denials, drain lag, and budget; `max_instance_count` per service (`infra/cloud_run.tf`). | Alerts fire in staging on a synthetic fault. |

Things that stay the same in Stage 1: the API contract, the web app's
views, and the invariants the tests encode. Those are single approval,
exact tuple binding, at most one payment, the untrusted runner and signed
receipts.

---

## 7. Quick reference

```bash
# local dev (two terminals)
.venv/Scripts/python -m uvicorn services.api.main:app --port 8000
cd web && npm run dev

# local, same-origin, exactly what the container runs
cd web && VITE_API_BASE_URL="" npm run build && cd ..
PAYGUARD_WEB_DIST=web/dist .venv/Scripts/python -m uvicorn services.api.main:app --port 8080

# tests
.venv/Scripts/python -m pytest -q

# container
docker build -t payguard-demo:local . && docker run --rm -p 8080:8080 payguard-demo:local

# smoke test any running instance
.venv/Scripts/python scripts/demo/record_demo.py --url http://127.0.0.1:8080
```
