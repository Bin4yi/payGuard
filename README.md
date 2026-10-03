# PayGuard

An AP2-inspired Gemini rent-payment prototype: a deterministic gateway
enforces that an autonomous agent can only execute the exact payment a user
approved. See [`docs/PROJECT.md`](docs/PROJECT.md) for the why/scope,
[`docs/SPEC.md`](docs/SPEC.md) for the implementation contract, and
[`docs/CLAUDE_CODE_PROMPT.md`](docs/CLAUDE_CODE_PROMPT.md) for the phased
build plan this repository follows.

**Status: Phases 1-4 implemented and tested** (deterministic core; signing
recovery + receipts; the runner/Gemini workflow behind a fake model client;
a FastAPI backend + React/TS/Vite frontend for isolated demo access).
**Phase 5 is partial and explicitly bounded by what this environment can
do:** no real GCP project, no Gemini API credentials, no `terraform` CLI.
What Phase 5 work *is* done: the SPEC.md Section 13 evaluation harness and
frozen 80-workflow corpus (a dry run against a naive stand-in model, never
to be read as a live-model result), the configuration A/B "broad simulator"
baseline, a code-level (not deployed-IAM) negative-access check for the
runner, and a Terraform scaffold for the split-service deployment
architecture — written but never `plan`ned or `apply`d. See "Explicitly not
evidence of" below before repeating any number this repository prints.

## Watch it

[![PayGuard promo (2:30)](docs/screenshots/01-welcome.png)](docs/demo/payguard-promo.mp4)

| Video | What it is |
|---|---|
| **[Promo + technical explainer (2:30)](docs/demo/payguard-promo.mp4)** | The breakthrough explained with diagrams and real app footage: signed mandate, 5-minute grant, exact-match gateway, exactly-once payment, signed receipt chain, photo notices, Laya, the Attack Lab and account changes. Voiceover + music. Built with HyperFrames in [`videos/payguard-promo/`](videos/payguard-promo/). |
| **[Narrated walkthrough (~2 min)](docs/demo/payguard-demo.mp4)** | The original captioned click-through, recorded and asserted by Playwright (`scripts/demo/record_demo.py`). |

GitHub doesn't play `.mp4` files inline from the repository; click a link to
open or download the video.

## Screenshots

| | |
|---|---|
| ![Landing page](docs/screenshots/01-welcome.png) **Landing page.** "The AI proposes. Only you approve." | ![Dashboard](docs/screenshots/02-dashboard.png) **Dashboard** with the five-step trust path. |
| ![Approval with photo OCR](docs/screenshots/03-approval-ocr.png) **Approve one exact payment.** A photographed notice is read on-device with OCR (92% confidence). | ![Agent activity](docs/screenshots/04-activity.png) **Scoped grant:** one job, one execution, live 5-minute countdown. |
| ![Poisoned notice stopped](docs/screenshots/05-result.png) **Poisoned notice stopped.** Laya scored it 0.94; nothing was paid. | ![Attack Lab](docs/screenshots/06-lab.png) **Attack Lab:** 10 attacks, agent fooled 10/10, LKR 0.00 reached the attacker. |

### Real-world test images

The photo upload and Attack Lab were tested on real scam images from
Wikimedia Commons, not only on images made for this project
([credits and licences](fixtures/real_notices/CREDITS.md)):

| Real fake-invoice scam (public domain) | Phishing email (CC BY 4.0) | Phishing SMS (CC BY-SA 4.0) | Printed poisoned notice (made for the demo) |
|---|---|---|---|
| ![Fake invoice](fixtures/real_notices/fake-invoice-bluffaktura.jpg) | ![Phishing email](fixtures/real_notices/phishing-bank-email.png) | ![Phishing SMS](fixtures/real_notices/phishing-sms.png) | ![Poisoned notice](web/public/samples/poisoned-rent-notice.png) |

## New: Attack Lab and photo notices

- **Attack Lab** (`/v1/lab/attacks`, `/v1/lab/run`, `services/lab/`): ten real
  scam techniques (prompt injection, hidden footer, zero-width and full-width
  characters, look-alike account, inflated amount, split payment, Sinhala
  instruction, "we changed banks") plus a genuine control notice, run against
  a deliberately *obedient* agent. Each defence is tested **on its own**, in a
  throwaway store that never touches the visitor's scenario: Laya alone, the
  runtime review gate alone, and the real gateway alone with a valid signed
  grant. Measured with local Laya: agent fooled 10/10; review gate stopped
  10/10; gateway refused 10/10; Laya flagged 9/10 (it missed the full-width
  attack at 0.46); control paid exactly once. You can also type your own
  attack or upload a photo of one.
- **Photo notice upload** (`web/src/components/PhotoNotice.tsx`): OCR runs in
  the browser (tesseract.js); only the extracted text is sent, and it is
  treated like any other untrusted document text. The server accepts
  `text/plain`, `image/png`, `image/jpeg` or `image/webp` as the media type,
  but only ever stores text.
- **Redesigned UI**: neumorphic controls with frosted-glass panels, a trust-path
  progress tracker, and dark mode.

## Layout

```
/services
  /authority   mandate lifecycle (draft/confirm/sign/revoke/replace), grant reservation+activation,
               scheduled signature recovery (services/authority/recovery.py)
  /gateway     the payment enforcement decision procedure (SPEC.md Section 7) — the only code
               path that can move money
  /runner      job-scoped documents, the five typed tools (SPEC.md Section 10), the
               ModelClient boundary (a FakeModelClient for tests; GeminiModelClient is a
               real-SDK adapter stub, not exercised — no live model access here), and the
               orchestrator that enforces the runtime review gate model output cannot clear
  /receipts    ordered chained receipt signing with a separate key (services/receipts/signing.py)
               and in-app verification with supporting-record digest recomputation
               (services/receipts/verify.py)
  /shared      strict schemas, canonical normalization/digests, the compact-JWS profile,
               the in-memory transactional store standing in for Firestore, local (test-only)
               signing/identity adapters (including a FlakySigner for simulating KMS outages
               and a LocalIdentityVerifier standing in for Firebase Auth)
  /api         the FastAPI demo backend (services/api/app.py) fronting all of the above for
               browser use, plus services/api/state.py's AppState wiring and the
               services/api/main.py uvicorn entrypoint
  /eval        SPEC.md Section 13 evaluation harness (services/eval/harness.py), the
               configuration A/B "broad simulator" (services/eval/broad_simulator.py) and its
               baseline agent orchestrator (services/eval/baseline_orchestrator.py) — all
               explicitly quarantined from the public demo API, never touched by real credentials
/web           React + TypeScript + Vite frontend: the six SPEC.md Section 14 views
               (dashboard, approval, agent activity, result, receipt trail, account-change
               review), talking only to the FastAPI backend's HTTP surface
/fixtures/eval the frozen 80-workflow corpus (corpus.json) and its generator script
/infra         Terraform scaffold for the split-service Cloud Run deployment — written, not
               run; see infra/README.md for exactly what has and hasn't been checked
/scripts/demo  record_demo.py — Playwright walkthrough that records docs/demo/payguard-demo.mp4
               and doubles as the browser end-to-end test
/docs          SPEC, PROJECT, the build prompt, PayGuard_Explained (.tex/.pdf guide), demo/
/tests
  /unit        normalization, digest, and JWS golden/tamper tests
  /security    scope mutation, credential validation, numeric parsing, ownership, funds
  /concurrency races, replay, idempotency
  /integration happy path, destination-change recovery, revocation/expiry ordering,
               signing recovery (KMS outage/restart/duplicate-worker), receipt chain
               signing + verification (tamper/truncation/gap detection), the runner
               orchestrator against a scripted FakeModelClient (normal/poisoned/refused/
               ambiguous/schema-repair scenarios), the full HTTP API (fresh-browser
               normal/blocked/replay/review flows, cross-session isolation, reset semantics),
               the broad simulator (accepts an otherwise-valid alternate destination while
               keeping every other control), and a fast subset run of the evaluation harness
```

## Running the tests

```
python -m venv .venv
.venv/Scripts/pip install -e ".[dev]"     # Windows; use .venv/bin/pip on macOS/Linux
.venv/Scripts/python -m pytest
```

198 tests, all passing as of this checkpoint (~30s; the evaluation-harness
tests run a small corpus subset — see below for the full 720-trial dry run).
They run fully offline: the Laya screener is a scripted fake in tests.

## Demo video

**[`docs/demo/payguard-demo.mp4`](docs/demo/payguard-demo.mp4)** — a
~2-minute narrated walkthrough recorded from the real app, following the
PROJECT.md Section 8 story: approve → the agent pays exactly once → signed
receipt trail and verified export → a replay that can't pay twice → a
poisoned notice stopped before any money moves → a genuine account change
that pays only after explicit confirmation. Screenshots of each scene are in
[`docs/demo/screens/`](docs/demo/screens/).

To re-record it (needs Node, and `ffmpeg` for the `.mp4`):

```
.venv/Scripts/pip install playwright gradio_client
.venv/Scripts/python -m playwright install chromium
.venv/Scripts/python scripts/demo/record_demo.py              # headless, records
.venv/Scripts/python scripts/demo/record_demo.py --headed     # watch it run
.venv/Scripts/python scripts/demo/record_demo.py --no-video --pace 0.2   # quick E2E check
```

The script builds the frontend, starts both servers itself, and **asserts the
expected outcome at every step** — so a run that shows the wrong thing fails
instead of producing a misleading video. It doubles as the browser end-to-end
test. The story checks for the Laya panel, so it needs a working screener
(`--screener laya-local` while the public Laya Space is paused).

## HTTP API

Every SPEC.md Section 12 endpoint now exists. Two caller kinds, never
interchangeable — a user session token is refused on service routes and a
service credential on user routes:

| Caller | Endpoints |
|---|---|
| User (session token) | `POST /v1/demo/scenarios`, `GET /v1/demo/scenarios/current`, `POST /v1/rent-instructions`, `POST /v1/documents`, `POST /v1/mandates`, `POST /v1/mandates/{id}/confirm`, `GET /v1/mandates/{id}`, `POST /v1/mandates/{id}/revoke`, `POST /v1/mandates/{id}/review`, `POST /v1/grants/{id}/revoke`, `POST /v1/jobs`, `GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/run` (demo-only trigger), `GET /v1/jobs/{id}/receipts`, `POST /v1/jobs/{id}/receipt-export` |
| Runner (service credential) | `GET /v1/internal/jobs/{id}/context`, `GET /v1/internal/jobs/{id}/grant`, `POST /v1/payments/execute` |
| Scheduler | `POST /v1/internal/authority/drain`, `POST /v1/internal/receipts/drain` |
| Authority | `POST /v1/internal/demo/scenarios/{id}/seed` |

`POST /v1/payments/execute` takes the grant in `X-PayGuard-Grant` and the key
in `Idempotency-Key`, parses the raw body with duplicate-key rejection before
any normal JSON handling, and maps reason codes to 200/403/409/422/503.
Every service-layer error maps to a typed HTTP error — none surfaces as a 500.

## Laya pre-screening (decision model)

Before Gemini reads a rent notice, a fast *decision model* —
[Laya](https://laya.convaiinnovations.com/) (ConvAI Innovations, Apache 2.0),
which uses the same question format as TypeSafe AI's hosted Jev — answers one
yes/no question: *does this text try to change the account or payee payment
goes to?* At probability ≥ 0.5 the job goes straight to human review, with no
Gemini call spent and no payment attempted.

It is **one-directional**: it can only *add* a review flag, never clear one or
approve anything. A missed attack falls through to Gemini, the review gate and
the gateway unchanged; a screener outage fails open to those same layers. So it
can cost a legitimate payment a review, but never cost security
(`tests/integration/test_screening.py` pins this, including a "fooled"
screener that scores an attack at 0.0).

| `PAYGUARD_SCREENER` | What runs |
|---|---|
| `laya-hf` (demo default) | Laya via the free public Hugging Face demo Space — `pip install -e ".[laya-hf]"`. No download, no key. A shared *demo*, not a paid API: rate-limited, can sleep, and notice text goes to a third-party host — fine for synthetic data only. Calls are capped at 8 s and back off for 60 s after a failure. |
| `laya-local` | Laya on this machine — `pip install -e ".[laya-local]"` (PyTorch + ~0.8 GB checkpoint). Nothing leaves the machine. |
| `none` | Off. The test suite always runs offline with a scripted fake. |

HF's serverless Inference API does not serve Laya ("isn't deployed by any
Inference Provider"); the demo Space's `/run_playground` endpoint is the free
hosted route. Jev has a stub adapter (same question format) but needs a paid
TypeSafe API key.

**Measured** on the frozen corpus (`python -m services.eval.screening_eval`,
threshold fixed beforehand, raw probabilities in `fixtures/eval/results/`):
30/30 poisoned notices flagged, 1/40 legitimate notices wrongly flagged,
4/4 genuine account changes sent to review, median 2.7 s. Caveat: the
corpus's attacks are templated; a hand-written subtle rewording ("remittance
details were updated: account 7712-0093") scored 0.19 and was missed — which
is exactly why this layer is advisory and the gateway remains the guarantee.

## Running the local demo end to end

```
# Terminal 1 — backend
.venv/Scripts/python -m pip install uvicorn
.venv/Scripts/python -m uvicorn services.api.main:app --port 8000

# Terminal 2 — frontend
cd web
npm install
npm run dev          # http://localhost:5173, or `npm run build && npm run preview`
```

The frontend reads `VITE_API_BASE_URL` (default `http://localhost:8000`).
The backend's CORS allows only the local Vite dev/preview origins by default
(`PAYGUARD_CORS_ORIGINS` overrides it; `*` is refused).

## Deploying

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**. Short version: the root
`Dockerfile` builds one container that serves the web app and API from the
same origin. Built and smoke-tested locally with Docker; it is deployable to
Cloud Run as a **single-instance** demo (`--max-instances 1`, because all
state is in process memory):

```
docker build -t payguard-demo:local . && docker run --rm -p 8080:8080 payguard-demo:local
.venv/Scripts/python scripts/demo/record_demo.py --url http://127.0.0.1:8080   # asserted smoke test
```

The guide also contains the Stage 1 plan: Firestore, KMS, Firebase Auth,
Gemini and four split services, with acceptance criteria for each item.

## Running the evaluation harness dry run

```
.venv/Scripts/python -m services.eval.harness
```

Takes ~3 minutes (720 trials, mostly RSA key generation). Prints a report
in the exact SPEC.md Section 13 shape (automatic completion, review
handling, unauthorized proposals vs. transfers) — read
`services/eval/harness.py`'s module docstring before quoting any number
from it: it runs against `NoticeParserModelClient`, not Gemini, and is not
a SPEC.md Section 13 evaluation result under any circumstance.

## What the deterministic core actually proves

- An authorized request pays exactly once; any altered field (destination,
  amount, currency, reference, debit account, obligation) blocks with no
  debit.
- 25 concurrent execution attempts against one obligation — whether with the
  same or different idempotency keys — never produce more than one physical
  transfer.
- Revocation and execution serialize correctly regardless of which commits
  first.
- A signed grant/mandate cannot be forged, replayed under another job, or
  reused past expiry; PSS-signature randomization means only the persisted
  token hash (not "any validly-signed token") is authoritative.
- The required destination-change recovery flow (blocked → review → explicit
  confirmation → old authority invalidated → new grant) works end to end.
- A simulated Cloud KMS outage during mandate/grant signing leaves state
  cleanly `PENDING` (nothing partially written); a scheduled recovery worker
  retries and converges, and two "duplicate worker" signers racing the same
  record never produce two accepted signatures.
- Receipts sign in strict sequence with a separate key, chain to the
  previous receipt's actual stored bytes, and in-app verification tells
  apart a merely-pending suffix, a removed middle event, a tampered
  signature, and a tampered exported supporting record (e.g. an altered
  payment_attempts row) — each with its own specific status, never silently
  conflated with "everything is fine."
- The runner's runtime review gate — not the model's judgment — decides
  whether `propose_payment` may even be attempted: a poisoned notice that
  fools the model into naming an attacker account either gets caught as a
  conflict before the gateway is ever asked, or (when the poisoning reaches
  the tool-call arguments directly) is blocked *by the gateway* with the
  model's actual attempted tuple preserved in evidence — never silently
  corrected to the approved one first. A model that resists the poisoning
  and proposes the approved tuple pays normally; that is not mislabeled as
  an authorization failure just because the input was adversarial.
- A fresh browser session (no prior account) can complete the normal,
  blocked, replay, and account-change-review flows entirely through the
  HTTP API; a second session's bearer token cannot read or confirm the
  first session's scenario, mandate, or job (404/409, never leaked state);
  and starting a fresh scenario ("reset") never rewrites a previous paid or
  in-flight obligation — verified directly against the store, not just the
  API's own claims.
- Against the same naive stand-in model, a baseline agent with no scope
  enforcement (configurations A/B) can be made to actually pay an
  attacker-chosen destination — proving the baseline is a meaningful
  comparison, not a strawman — while the identical scenario under
  PayGuard's real gateway (configuration C) produces zero unauthorized
  proposals and zero unauthorized transfers. This is a harness-mechanism
  result, not a live-model evaluation result (see below).

## Explicitly not evidence of

Cloud authentication, Cloud KMS behavior, Firestore's actual transaction
semantics, actual Firebase Auth, or **actual Gemini behavior**. The local
store, local signing/identity adapters, and `FakeModelClient` exist solely
to test the state machine, binding rules, and runner orchestration logic in
isolation (see `services/shared/local_adapters.py`,
`services/shared/identity.py`, `services/shared/store.py`, and
`services/runner/model_client.py` for the scope of what they stand in for).
`GeminiModelClient` and `FirebaseIdentityVerifier` are real-SDK adapter
stubs for later deployment; both raise `NotImplementedError` and neither is
called by any test here. Live-model results (SPEC.md Section 13's A/B/C
evaluation) require wiring `GeminiModelClient` up against a pinned model ID
with actual API credentials, then running the frozen evaluation corpus —
neither of which has happened yet.

The `NoticeParserModelClient` that powers `POST /v1/jobs/{id}/run` in the
demo is a deliberately naive regex-based stand-in, not an approximation of
Gemini's actual document understanding — it exists only so the demo has
something to show without API credentials, and is honored as susceptible to
an explicit "PAY INSTEAD TO: ..." override phrase specifically so the
poisoned-notice attack scenario has something concrete for the gateway to
stop. Its behavior says nothing about how a real model would or wouldn't
resist a given prompt injection.

**The frontend (`/web`) is exercised in a real browser** by
`scripts/demo/record_demo.py` (Playwright + Chromium), which clicks through
every demo flow and asserts the visible outcome at each step. What that does
*not* cover: keyboard-only navigation, screen-reader behaviour and colour
contrast are implemented per SPEC.md Section 14 but have not been audited
with assistive technology.

**Phase 5 is the least complete phase, and deliberately so** — it requires
resources this environment does not have:

- **No real Google Cloud project.** `infra/*.tf` is written (and checked
  for HCL syntax validity only, via `python-hcl2` — never `terraform
  validate`d, `plan`ned, or `apply`d) but provisions nothing. The
  application code is still the Phase 1-4 single-process
  `services/api/app.py`, not four independently deployable Cloud Run
  services that only call each other over verified HTTP — `infra/cloud_run.tf`
  describes the target split architecture, not what's actually running.
- **No deployed IAM to test against.** `tests/security/test_runner_negative_access.py`
  is the honest substitute: static proof that `services/runner` has no
  Python import path to any signing capability. That is real evidence at
  the code level; it is not evidence about a deployed service account's
  actual IAM permissions.
- **No Gemini API credentials.** `services/eval/harness.py` and
  `fixtures/eval/corpus.json` are real, tested, and ready — but every
  number they produce comes from `NoticeParserModelClient`, and the
  harness's own docstring says outright that configurations A and B are
  expected to be *identical* in this dry run, because the stand-in model
  has no way to be sensitive to a prompt's safety instruction. The actual
  SPEC.md Section 13 evaluation (720 live-model trials, A vs. B vs. C
  meaningfully different) has not been run and cannot be run here.
