# PayGuard — Technical Specification

**Version 2.1 · 13 September 2026**

Changes from 2.0: corrected AP2 positioning (§2), restored threat model and transaction checks
(§4, §7), specified signing lifecycle (§5-6), retained account-change recovery (§5), required
pinned-key in-app verification (§9), defined controlled evaluation (§13) and isolated demo
sessions (§14). This is a frozen MVP build specification, not claimed implementation or results.

---

## 1. Terms

| Term | Meaning here |
|---|---|
| **Mandate** | The server-side record of what the user approved. Immutable once approved. Signed by the authority as its observation of user approval; an AP2-inspired private profile. |
| **Grant** | A short-lived signed token given to the agent runner, carrying the mandate's scope. |
| **Obligation** | One thing that must be paid once — e.g. `rent-2026-09`. Server-created. The agent cannot invent one. |
| **Runner** | The Cloud Run service that calls Gemini. Untrusted for authorisation purposes. |
| **Gateway** | The only service that can write to the ledger. |
| **Tuple** | The exact payment identity: action + debit account + destination + version + amount + currency + reference + obligation. |

---

## 2. AP2 positioning

AP2 specifies mandate delegation, action authorization, constraint verification and signed
acceptance/rejection receipts. Its threat model includes manipulated agents. PayGuard does
not claim these mechanisms are missing from AP2.

| Existing AP2 concept | PayGuard MVP |
|---|---|
| User authority expressed by a mandate | Private `payguard-mandate+jwt` signed by the authority after authenticated confirmation |
| Verifier checks integrity and permitted action | Gateway checks the actual payment tuple and transactional state |
| Signed authorization receipt and structured errors | Private signed service-event chain, evidence display and explanations |
| Checkout/payment mandates and participant/credential contracts | Not implemented; synthetic rent transfers only |

Public description: PayGuard is an AP2-inspired Gemini rent-payment prototype that evaluates
exact-scope enforcement, duplicate prevention, failure recovery and user-readable evidence.
It uses private JWS profiles and makes no AP2 conformance or interoperability claim. JWS and
similar vocabulary alone do not implement AP2. OAuth token exchange remains later work; the
MVP issues grants directly. The contribution is evaluated integration, not a new authorization
primitive or exclusive explanation mechanism.

Primary references:

- https://ap2-protocol.org/ap2/agent_authorization/
- https://ap2-protocol.org/ap2/security_and_privacy_considerations/

Before future interoperability work, pin the AP2 version/commit, map its required fields and
verification rules, and test against the corresponding samples.

---

## 3. Requirements

| ID | Behaviour | Acceptance evidence |
|---|---|---|
| F01 | Authenticate the user, create a rent instruction | Another user cannot read or approve it |
| F02 | Show and approve exact payment details | Stored mandate matches the displayed version |
| F03 | Issue a bounded grant to the runner | Agent cannot mint, edit or renew it |
| F04 | Read a selected PDF or email with Gemini | Structured proposal includes source references |
| F05 | Check every attempted payment at the gateway | Altered account or amount produces no debit |
| F06 | Prevent duplicates across retries and new grants | One obligation → at most one ledger transfer |
| F07 | Record and sign consequential events | Export verifies against pinned public keys |
| F08 | Support revocation and reviewed account changes | Old scope cannot execute after committed revocation |
| F09 | Show completed, blocked and pending states | A timeout never displays as a confirmed failure |
| F10 | Deploy on Google Cloud | Browser demo works through the deployed backend |

---

## 4. Architecture and trust boundaries

The browser calls the authority with a Firebase ID token. The authority records approval,
signs mandates and grants using the authority KMS key, and assigns a job to the runner. The
runner calls Gemini and submits proposals to the gateway. The gateway verifies authority
and commits the synthetic transfer plus an event outbox in Firestore. The receipt worker
signs outbox events with a separate receipt KMS key. The browser reads owned results through
the authority; neither the browser nor the runner accesses Firestore directly.

| Service | May do | May NOT do |
|---|---|---|
| Browser | Call authority with a verified user token | Touch Firestore directly |
| Authority | Read/write instructions, mandates, grants and jobs; sign mandates and grants | Write the ledger |
| Runner | Call Gemini, read assigned documents, call typed tools | Write Firestore, access any KMS key, call a hidden bank path |
| Gateway | Write ledger, obligation state, grant state, event outbox | Sign receipts |
| Receipt worker | Read outbox, sign, write receipts | Write ledger or mandates |

Separate service accounts per service. No downloadable long-lived keys. Internal calls use
Google-issued ID tokens validated for the intended audience and allowed principal.

**Firestore server SDK access uses IAM and is not protected by client Security Rules.**
Ownership and operation checks must be enforced in server code. The permission table states
service responsibilities; it is not a claim of collection-level Firestore IAM isolation between
all trusted services. The runner has no Firestore permissions. Authority, gateway and receipt
service code are inside the trusted computing boundary.

### Threat model

An attacker can control selected documents, induce arbitrary model proposals, alter tool
arguments, replay requests and race execution. The runner is untrusted to choose payment scope.
The payment guarantee does not depend on Gemini recognizing an attack.

Assume the approval interface and user session, authority, gateway, signer, database state and
cloud IAM remain trusted. The attacker cannot forge signatures or create fresh authenticated
approval. The system cannot establish account ownership, document truth or user understanding;
an approved wrong account remains an approved wrong account. A compromised runner's access to
its assigned documents is outside the payment-scope guarantee.

### Required security invariants

| ID | Property |
|---|---|
| S01 | Each new transfer matches a valid active grant and its signed approved mandate, including the actual payment tuple |
| S02 | One server-created obligation has at most one committed transfer across retries, jobs and replacement grants |
| S03 | User, job, grant, mandate, obligation and runner bindings agree; content cannot create or widen authority |
| S04 | Revocation/supersession and execution serialize on the same authoritative records |
| S05 | Every committed transfer includes its durable event in the same transaction |
| S06 | Verification uses pinned trusted keys, exact stored bytes and the correct signature profile |
| S07 | Documents, jobs, approvals, grants and receipts are scoped to their owner and assigned workflow |

These are conditional design properties to implement and test. Finite tests do not prove the
entire deployed system secure.

---

## 5. Approval and mandate

Before a grant is issued, the authority creates an immutable mandate snapshot:

```
schema_version, mandate_id, mandate_version, user_id, instruction_id, instruction_version,
obligation_id, action, debit_account_id, payee_id,
destination_account_id, destination_version,
amount_mode, amount_minor, currency, payment_reference,
max_executions, grant_expiry_seconds, selected_document_ids,
policy_version
```

The confirmation record adds `approved_at`; digest and signature are stored outside the signed
payload to avoid a circular hash. Map payload `user_id` explicitly to storage `owner_uid`.

Rules:

1. The **server** creates consequential identifiers and checks ownership. The browser may return existing server-issued IDs; it cannot assign obligation IDs, grant IDs or destination versions.
2. The browser renders the exact snapshot and posts back its ID, version and a one-time nonce.
3. The authority verifies a current authenticated session and atomically marks that version
   approved. A changed snapshot requires a new confirmation.
4. A payee display name **never** substitutes for a destination account identifier.
5. Model suggestions stay drafts until explicit user approval. The initial approved destination
   comes from the user's rent instruction, not automatic endorsement of document text.

### Signed mandate lifecycle (required)

The authority signs its record of authenticated confirmation. This is not a user-held-key
signature or proof independent of the trusted authority.

1. Freeze a `DRAFT` snapshot with a confirmation nonce. Display all consequential fields.
2. Confirm with the owning Firebase identity, snapshot version and unused nonce. In a transaction,
   consume the nonce, record `approved_at`, set mandate `APPROVED` and `signing_state=PENDING`,
   and append the approval outbox event. For the initial mandate, atomically check the unpaid
   obligation has no current approved mandate and install its current mandate pointer; later
   approvals must use the replacement path below. Repeated confirmation returns existing status.
3. Outside the transaction, sign the fixed payload using compact JWS: `alg=PS256`,
   `typ=payguard-mandate+jwt`, configured authority `kid`. The payload contains `iss=payguard-authority`,
   `schema_version=mandate-v1`, the snapshot fields above, and `approved_at` as integer Unix
   seconds. Exclude nonce, signature, digest and mutable lifecycle state from the signed payload.
4. Serialize under §9. Define `payload_digest` as SHA-256 of ASCII `mandate-v1`, one zero byte,
   then exact payload bytes. Store the compact JWS, digest and key ID. Compare-and-set only if
   that payload is still approved and pending; discard an unstored signature if revocation or
   supersession has won the race.
5. The gateway verifies the stored mandate JWS using independently configured authority public
   keys and a mandate-specific validator. Compare its payload to the approved snapshot and its
   digest to the grant. The signature never overrides live revocation state.

Mandates and grants share one authority KMS key in the MVP, with distinct types, schemas and
validation entry points. Receipts use a second KMS key. Neither browser nor runner can invoke
either key. Never accept a mandate or receipt where a grant is required.

On KMS or persistence failure, keep signing `PENDING`, retry the same frozen payload with
bounded backoff and show status. A trusted scheduled authority worker recovers pending work
after restarts; a best-effort immediate attempt improves latency. **No grant activates until
mandate signing completes.** Do not ask the model to repair or reapprove a signing failure.
Grant preparation and activation follow §6.

### Amount handling

v1 uses **exact mode**: `amount_minor == 5000000`, `currency == "LKR"`, no separate fee.
Integers only. Reject fractions, floats, strings, booleans, zero, negatives, and anything above the demo
ceiling of `100000000` minor units.

Maximum mode is optional. If added, the approval screen must visibly say "up to", execution must
satisfy `0 < amount_minor <= max_amount_minor`, and it still permits one transfer only. Never
reuse an exact-mode mandate for maximum mode.

### Destination changes (required MVP recovery)

Blocked → review changed details → explicit confirmation → old authority invalidated →
replacement signed → new job/grant for the same unpaid obligation.

Create a new immutable destination version and draft mandate linked to the old mandate.
Show old and new account details and the unchanged amount, period and obligation. Treat model
and document claims as untrusted suggestions. Only the authenticated user can review/confirm.

On confirmation, one transaction checks ownership, nonce, current mandate and unpaid obligation;
supersedes the old mandate, revokes its pending/active grant if present, records the newly
approved snapshot, updates the obligation's current mandate pointer and appends events.
Keep at most one pending/active grant per obligation so this update is bounded. Execution reads
the same pointer and records. If payment commits first, replacement confirmation fails with
`OBLIGATION_PAID`; if confirmation commits first, execution under old scope fails.

Sign outside the transaction. Signing failure leaves the replacement pending and old authority
invalid. Recovery never reactivates old scope. Expiry recovery follows the same confirmation
flow with unchanged payment details and a new mandate ID; starting a job cannot silently renew
an expired grant. All replacements share the original obligation. v1 does not verify ownership
of a newly supplied account.

---

## 6. Grant token profile

Signed JWS, `alg: PS256`, `typ: payguard-grant+jwt`, allowlisted `kid`, Cloud KMS RSA-PSS-SHA256.

```json
{
  "iss": "payguard-authority",
  "aud": "payguard-payment-gateway",
  "sub": "demo-user-01",
  "jti": "grant-01",
  "iat": 1789344000,
  "nbf": 1789344000,
  "exp": 1789344300,
  "agent_principal": "configured-runner-service-id",
  "mandate_id": "mandate-01",
  "mandate_version": 1,
  "mandate_digest": "sha256:example-digest",
  "job_id": "job-01",
  "obligation_id": "rent-2026-09-01",
  "action": "rent.pay",
  "debit_account_id": "acct-tenant-01",
  "destination_account_id": "acct-landlord-01",
  "destination_version": 1,
  "amount_mode": "exact",
  "amount_minor": 5000000,
  "currency": "LKR",
  "payment_reference": "RENT-2026-09-01",
  "max_executions": 1,
  "policy_version": "rent-v1"
}
```

Validation:

- Accept only the configured algorithm, issuer, audience, token type, policy version and known
  key IDs. **Never** fetch verification keys from a URL inside the token.
- Maximum lifetime 300 seconds, enforced from server time. Allow ≤ 30 seconds skew on `iat`
  and `nbf`. Never extend `exp` for execution.
- The authenticated runner principal must equal `agent_principal`.
- Keep the token out of prompts, browser storage, document content and logs.
- Bind subject, grant ID, job, mandate ID/version/digest, obligation, policy, payment scope and
  runner to the stored records. Claims cannot select an unrelated task or approved mandate.

### Grant activation and retry contract

`POST /jobs` requires the owner and a signed approved mandate for an unpaid obligation.
In a transaction reserve a server-created job and one `PENDING_SIGNATURE` grant, bind them to
the obligation and assigned runner, and freeze the grant payload including issuance/expiry.
Atomically install the reserved current grant pointer on the obligation. Each mandate may issue
at most one grant. Repeated job creation returns the same job/status.
A change to selected documents requires a new approved snapshot.

Sign outside the transaction. In a second transaction recheck mandate state/signature digest,
obligation pointer/unpaid state, job assignment and expiry, then persist the exact token/hash,
activate the grant and append issuance evidence. If the frozen token expires before activation,
require fresh user confirmation; never extend its times or silently mint a replacement.
Signing/persistence failures remain pending and retry the same payload. A losing signer
discards its unstored signature. Recover pending grants through the authority worker in §5.

Start the runner only after activation. The private grant endpoint returns only the persisted
token to its assigned job/runner; it cannot create, renew or widen authority. Token lifetime
starts at job reservation, not while the user reads the initial approval screen.

---

## 7. Gateway enforcement

### Input

Typed request: `job_id, mandate_id, obligation_id, debit_account_id, destination_account_id,
destination_version, amount_minor, currency, payment_reference`. The only idempotency key is the `Idempotency-Key` header; reject a
duplicate body field. This endpoint fixes `action=rent.pay`.
The runtime attaches the grant header and its own service identity.

**Reject unknown fields.** An ignored field must never gain meaning later.

### Normalisation

Server-defined account IDs. Exact string equality for currency and reference. Integers for money.
Resolve the destination once from the approved immutable version and use that same value at
execution. Display-name similarity and model judgement **never** authorise equivalence.

### Decision procedure

1. Verify service identity and grant signature, type, issuer, audience, required claims, policy
   and lifetime. Compare the verified principal to `agent_principal`. Reject invalid credentials
   before private task reads; never trust a caller-supplied principal string.
2. Validate the request schema. Load only records selected through the verified grant, then
   check ownership/job assignment. Verify the stored mandate JWS under §5.
   Define the semantic payment tuple as action, debit account, destination account/version,
   amount/mode, currency, reference and obligation. Hash its versioned normalized form as
   `request_digest`; exclude token, idempotency key, job and mandate IDs. Store the full actual
   attempted request separately, including those context IDs. Hash that versioned attempt record
   as `attempt_digest` so evidence also binds the context fields excluded from `request_digest`.
3. Require agreement among actual request, signed grant claims, stored grant record and signed
   mandate snapshot. Compare payment fields, execution limit and policy. Require matching owner
   (`sub`), job, runner, mandate ID/version/digest, grant ID and persisted token hash.
   An unrelated valid grant cannot select another approved snapshot.
4. In the Firestore transaction, re-read mandate, grant, job, obligation, idempotency record,
   immutable destination and balances. Recheck bindings, tuple equality, state and server time
   on every retry. The exact signed payload/JWS digests verified outside must be unchanged;
   otherwise abort and restart verification outside the callback. Finish reads before writes.
5. An existing idempotency record under `(owner_uid, obligation_id, key)` with a different
   semantic digest returns `IDEMPOTENCY_CONFLICT`. If the obligation is already paid, return
   its result only for an authenticated matching retry with the same semantic tuple and
   originating job/mandate/grant. A new key can retrieve the same result; a different tuple or
   context returns conflict. A consumed grant has this read-only retry path only. Expired or
   revoked credentials cannot use execute even for lookup; the owning user retains the status
   API. No branch produces a second debit.
6. For **new** execution require grant `ACTIVE`, mandate `APPROVED` with signing `SIGNED`,
   current mandate/grant pointers on the obligation, matching READY/RUNNING job, unpaid
   obligation, unexpired grant, valid destination and sufficient funds. Pending, consumed,
   revoked, expired or superseded authority cannot execute.
7. Atomically debit/credit, create the transfer, mark obligation paid and grant consumed,
   persist idempotency result and actual attempt, and append the decision/result outbox event.
   Update the mandate's execution marker in the same transaction so revocation/execution
   conflict on it; never modify its signed snapshot.
8. Return payment `COMMITTED` with receipt `PENDING`. For an authenticated denial, persist
   the owned blocked attempt and its outbox event atomically before returning an audited
   refusal. If required evidence persistence fails, return `AUDIT_UNAVAILABLE` and make no
   new transfer. Unauthenticated malformed traffic can use infrastructure logs instead of
   private receipts.

**Never call Gemini, KMS or another external API inside a transaction callback.** Transaction
reads/writes are the intended database operations; model or signing side effects would repeat
when Firestore retries the callback.

### Reason codes

`INVALID_GRANT`, `GRANT_EXPIRED`, `GRANT_REVOKED`, `PRINCIPAL_MISMATCH`, `SCOPE_MISMATCH`,
`DESTINATION_CHANGED`, `IDEMPOTENCY_CONFLICT`, `OBLIGATION_PAID`, `INSUFFICIENT_FUNDS`,
`AUDIT_UNAVAILABLE`, `STATUS_PENDING`. Use `INVALID_GRANT` for context/token binding failures
and `GRANT_REVOKED` for revoked/superseded authority; preserve more precise details in owned
evidence without leaking another user's record existence.

User-facing text is generated from these facts. A blocked attempt cannot automatically request a
broader grant or restart the payment loop.

---

## 8. State and duplicate prevention

| Entity | States |
|---|---|
| Mandate | `DRAFT` → `APPROVED` → (`REVOKED` \| `SUPERSEDED`); separate signing `PENDING` → `SIGNED` |
| Grant | `PENDING_SIGNATURE` → `ACTIVE` → `CONSUMED`; pending/active can become `REVOKED`; expiry derives from time |
| Obligation | `UNPAID` → `PAID`, in the same transaction as the transfer |
| Job | `WAITING_SIGNATURE` → `READY` → `RUNNING` → (`DONE` \| `REVIEW_REQUIRED` \| `FAILED`); status never overrides payment authority |
| Attempt | `RECEIVED` → (`BLOCKED` \| `COMMITTED`) |
| Receipt event | `PENDING` → `SIGNED` |

**Two different replay controls, both required:**

- **Idempotency key** — handles retries of the *same* request. Stores request digest + transfer ID.
- **Obligation lock** — prevents a second payment with a *different* key, another valid token, or
  a replacement grant.

User revocation invalidates the current mandate and its pending/active grant together and
records an event. It cannot erase a committed payment. Revocation and execution write the same
grant/mandate records and read the obligation pointer, so they serialise. Whichever
commits first wins. We check time immediately before writing on each attempt; we do not claim
exact wall-clock ordering at the expiry boundary.

---

## 9. Receipts

### What gets signed

Mandate approval, grant issuance, grant revocation, authenticated payment attempts and decisions,
committed transfers, workflow completion.

Document reads and model proposals are recorded as **activity** with source IDs and hashes, and
labelled as untrusted model assertions rather than trusted service observations.

```json
{
  "schema_version": "receipt-v1",
  "iss": "payguard-receipt-worker",
  "receipt_id": "receipt-04",
  "workflow_id": "workflow-01",
  "sequence": 4,
  "event_type": "payment.blocked",
  "event_producer": "payment-gateway",
  "signing_service": "receipt-worker",
  "occurred_at": "2026-09-14T00:02:10Z",
  "signed_at": "2026-09-14T00:02:11Z",
  "grant_id": "grant-01",
  "mandate_digest": "sha256:...",
  "request_digest": "sha256:...",
  "attempt_digest": "sha256:...",
  "decision": "BLOCK",
  "reason_codes": ["SCOPE_MISMATCH"],
  "payment_id": null,
  "previous_receipt_digest": "sha256:..."
}
```

Rules: compact JWS, `alg: PS256`, `typ: payguard-receipt+jwt`, allowlisted receipt `kid`, separate key from the grant signer. Sign the exact
payload bytes. Normalise as sorted-key UTF-8 JSON without whitespace, ASCII identifiers, integers
only. Hash the ASCII schema-version string, one zero byte, then those bytes. Use `payment-tuple-v1`
for semantic payment digests; a digest never appears inside its own input. Reject duplicate keys. Test with golden
fixtures. **Never accept a receipt as a grant.**

For attempt evidence, use `attempt-v1` and sign a digest of the normalized record containing
the owner, attempt/job/mandate/grant IDs, actual typed payment fields, idempotency key and
semantic request digest. Exclude credentials and the attempt digest itself. Preserve denied
field values, never a silently repaired request. Only the semantic digest is used for payment
idempotency; the attempt digest protects the full exported context. Invalid-schema requests
use a separate bounded rejection record, not a fabricated valid tuple. Never export tokens.

### Reliability

Sequence numbers are assigned in the same transaction that creates the outbox event. The worker
signs the next unsigned sequence only, includes the previous JWS digest, signs outside the
transaction, then compare-and-sets the expected chain head before storing.

The UI may show a committed payment while receipts are pending. **Never report cryptographic
verification until a verifier has actually checked the stored bundle.**

### In-app verification (required)

Verify signatures, profile/type, issuer, sequence and previous stored-JWS digests against
exact stored bytes. Install allowlisted public keys through trusted app configuration,
independently of any export. A key included in the export is not a trust root.

Export signed receipts, the referenced signed mandate and actual request records. Recompute
supporting-record digests so approved-versus-attempted details are checked rather than merely
displayed. Altering an unsigned supporting record must fail its referenced digest check.
Distinguish `SIGNED` (signature stored) from `VERIFIED` (verifier checked it). Show verification
failure and completeness unknown explicitly. A truncated suffix can still have valid signatures.
Golden fixtures, type-confusion and tamper tests remain mandatory.

### Optional (later phase — cut first)

Final signed checkpoints, the standalone offline verifier binary, external anchoring.
v1 ships in-app verification of a single receipt chain and is honest about the limit:
a chain detects modification and internal gaps; it cannot alone prove nothing was withheld.

---

## 10. Gemini workflow

The model interprets documents and explains results; deterministic code handles exact checks
and authorization. Show the work saved after approval and measure quality before claiming gains.

1. **Read and reconcile** — interpret a PDF or email notice; extract period, amount, destination;
   link extracted fields to a page or text span and supporting quote; compare against the approved mandate **and**
   the obligation's payment history.
2. **Detect and surface conflict** — report disagreements as structured conflicts with evidence,
   instead of silently choosing one side.
3. **Propose narrowly** — one typed payment proposal via function calling, strict schema.
4. **Explain** — turn gateway reason codes into plain language, and draft the account-change
   review for the user to approve or reject.

### Output contract

```json
{
  "schema_version": "rent-proposal-v1",
  "document_id": "notice-01",
  "rent_period": "2026-09",
  "claimed_amount_minor": 5000000,
  "claimed_currency": "LKR",
  "claimed_destination_text": "DEMO-LANDLORD-001",
  "evidence": [
    {"field": "claimed_amount_minor", "page": 1, "quote": "Monthly rent LKR 50,000.00"}
  ],
  "conflicts": [],
  "next_action": "propose_payment"
}
```

### Runtime checks

- Validate against a strict schema. Missing or ambiguous values become `null` and require
  clarification. One schema-repair retry, then stop with a clear error.
- Resolve document IDs and pages against actual job inputs. Validate quoted text where extracted
  text allows. **A model citation is an assertion until checked.**
- Never send tokens, signing material, sessions, unrelated accounts or unrelated documents
  to Gemini.
- Limits: 3 documents, 10 pages each, 5 MB per file, 8 tool calls, 60-second task budget.
- Pin the model ID, SDK version and prompt version before evaluation. No `latest` alias,
  no unverified preview names.

### Tools

| Tool | Input | Effect |
|---|---|---|
| `get_rent_context` | `job_id` | Owned mandate scope + obligation status |
| `read_selected_document` | `job_id, document_id` | Permitted document or evidence view |
| `propose_payment` | typed payment fields | Pass the exact tuple to the gateway |
| `get_payment_status` | `job_id` | Authoritative transfer + receipt state |
| `explain_payment_result` | `decision_id` | Explanatory text from structured facts |

No tool accepts a URL, shell command, SQL expression, callback destination or raw HTTP request.
**Never expose approval, new-payee creation or token issuance as model tools.**

**Do not silently replace a wrong destination with the approved one and then claim the attack
was blocked.** Preserve the model-proposed fields in the attempt receipt. v1 stops on conflict
and uses the required review flow in §5. If Gemini refuses before a gateway call, record model
activity, not a fabricated payment attempt. If it proposes the approved tuple despite poisoned
text, any gateway execution must still remain within scope; do not expect every poisoned input
to cause zero transfers. A deterministic review flag raised by validated workflow checks stops
automatic submission. Measure conflict detection separately from actual payment authorization.

---

## 11. Data model (Firestore)

| Collection | Key fields | Constraint |
|---|---|---|
| `users` | uid, display_name | Identity from verified auth |
| `accounts` | account_id, owner_uid, currency, balance_minor, version | Only the gateway mutates balances |
| `destinations` | destination_id, version, owner_uid, demo_account_id, label | Immutable version |
| `rent_instructions` | instruction_id, owner_uid, version, destination_ref, amount, currency | Agent cannot update |
| `obligations` | obligation_id, instruction_id, period, state, payment_id, current_mandate_id, current_grant_id | Stable ID; one pending/active grant |
| `mandates` | mandate_id, version, owner_uid, snapshot, digest, nonce_hash, approved_at, state, signing_state, compact_jws, key_id, issued_grant_id, execution_marker | Immutable signed payload; lifecycle separate |
| `grants` | grant_id, mandate_id, mandate_digest, job_id, obligation_id, principal, frozen_payload, compact_jws, token_hash, expiry, state | One grant per mandate; online activation/consumption |
| `jobs` | job_id, owner_uid, mandate_id, obligation_id, allowed_documents, grant_id, runner, status | Context cannot cross owners or assignments |
| `documents` | document_id, owner_uid, object_ref, digest, media_type, page_count | Ownership + size checked before use |
| `payment_attempts` | attempt_id, owner_uid, request, request_digest, attempt_digest, decision, reason_codes | Stores actual tuple and bound attempt context |
| `idempotency` | key, owner_uid, obligation_id, request_digest, payment_id | Same key + different body = conflict |
| `ledger_transfers` | payment_id, debit, credit, amount, currency, obligation_id | One transfer per obligation |
| `workflow_events` | workflow_id, sequence, producer, payload, signing_state | Durable ordered outbox |
| `receipts` | workflow_id, sequence, compact_jws, digest, key_id | Immutable after commit |

Monetary fields are integers. Timestamps come from trusted servers. Approved payloads and
destination versions are immutable — amendments create new versions.

Retention for demo data: delete uploaded documents after 7 days, operational logs after 30 days.
Never log bearer tokens, raw account secrets, full documents, or prompts containing personal data.

---

## 12. API

All paths under `/v1`. Public endpoints verify the Firebase session and ownership. Internal
endpoints require a verified service principal plus job or grant authorisation. Responses carry
`schema_version`, `request_id`, `status`, and a typed result or error.

| Method + path | Caller | Result |
|---|---|---|
| `POST /rent-instructions` | User | Create a validated draft |
| `POST /mandates` | User | Return immutable details + confirmation nonce |
| `POST /mandates/{id}/confirm` | User | Confirm exact version; begin/recover signing; pending is not executable |
| `GET /mandates/{id}` | User | Owned approval and signing status |
| `POST /mandates/{id}/revoke` | User | Revoke current approval and its pending/active grant, including before signing finishes |
| `POST /mandates/{id}/review` | User | Replacement draft for account change or expiry; no approval yet |
| `POST /grants/{id}/revoke` | User | Revoke current mandate and pending/active grant if execution has not committed |
| `POST /documents` | User | Validate and store an upload |
| `POST /jobs` | User | Reserve job/grant for signed approval; repeats return same job; activate after signing |
| `GET /jobs/{id}` | User | Task, payment and receipt state |
| `GET /jobs/{id}/receipts` | User | Ordered owned evidence |
| `POST /jobs/{id}/receipt-export` | User | Private downloadable bundle |
| `GET /internal/jobs/{id}/context` | Runner | Assigned context + evidence references |
| `GET /internal/jobs/{id}/grant` | Runner | Existing assigned token, **no renewal** |
| `POST /payments/execute` | Runner | Gateway validates and commits or blocks |
| `POST /internal/receipts/drain` | Scheduler | Process bounded pending events |
| `POST /internal/authority/drain` | Scheduler | Recover pending signatures; cannot create approval |
| `POST /demo/scenarios` | Anonymous-authenticated user | Create/reset this session's scenario with a fresh obligation |
| `POST /internal/demo/scenarios/seed` | Authority only | Idempotently seed balances for a new owned scenario through the gateway; cannot alter existing balances |

`POST /payments/execute` requires a service identity credential, an `X-PayGuard-Grant` header and
an `Idempotency-Key`. Status codes: `200` committed, `403` denied authority, `409` idempotency or
obligation conflict, `422` invalid business input, `429` rate limited, `503` state unavailable.

Confirmation and job creation return `202` with explicit signing status while pending;
repeated calls recover existing status. Approval signing, grant readiness, payment completion
and receipt verification remain separate states.

**An `ALLOW` decision is not the same as a completed payment.** Status retrieval stays available
to the owning user after grant expiry.

---

## 13. Evaluation

Gateway tests submit forbidden requests **directly**, so coverage never depends on persuading
Gemini to misbehave. Use a fake clock for boundary tests; record real deployed checks separately.

| Test family | Cases | Required outcome |
|---|---|---|
| Scope mutation | Payee, destination version, debit account, amount, currency, reference, obligation | No unauthorised transfer |
| Credential validation | Grant/mandate tamper, wrong key/type/audience/issuer/caller, missing fields, swapped job/mandate/grant, token-hash mismatch | Reject before ledger mutation |
| Numeric parsing | Negative, zero, float, string, boolean, excess value, duplicate JSON keys | Reject ambiguous input |
| Replay | Same request+key, changed body + key reuse, new key, replacement grant | ≤ 1 transfer per obligation |
| Races | 25 simultaneous requests per obligation × 20 repeats | Exactly one transfer |
| Revocation | Revoke before execute; race with execution | Committed order decides |
| Expiry | At, after, renewed, transaction retry boundary | No new execution after the check fails |
| Failure recovery | Response loss, DB error, worker crash, mandate/grant/receipt KMS outage, revoke-during-signing, expiry-before-activation | Status recovers, no duplicate debit |
| Ownership | Cross-user document, job, grant, receipt IDs | No cross-user access |
| Evidence tamper | Change payload, remove middle event, swap keys, truncate | Detect change or report completeness unknown |

### Frozen evaluation corpus

**80 held-out synthetic workflows:**

- 40 eligible for automatic payment: 30 straightforward, 10 varied layouts.
- 10 legitimate review-required cases: 4 genuine account changes, 3 ambiguous notices and
  3 expired-grant recoveries. Include one rejected change and one signing-delay recovery.
- 30 poisoned notices, five per family: account replacement, fake system instructions,
  urgency pressure, tool-format impersonation, false approval claims and retry manipulation.

Use separate development fixtures for iterative single runs. Freeze the corpus, expected
outcomes, model ID, SDK, prompt versions, decoding settings, tool schemas and release commit
before final evaluation. Run each workflow three times per configuration: 240 trials each,
720 total. Do not tune on the held-out set. Freeze scripted user responses for review cases
so every configuration receives the same confirmations/rejections. Report all runs.

### Controlled configurations

| Configuration | Prompt | Payment boundary |
|---|---|---|
| A — broad permission | Common task prompt | Authenticated isolated simulator without mandate tuple enforcement |
| B — prompt-only protection | Same prompt plus a fixed instruction to respect approved scope | Same broad simulator as A |
| C — PayGuard | Identical prompt to B | Constrained gateway in §7 |

A→B changes only the safety instruction. B→C changes only boundary enforcement. Keep model,
documents, context/history, tool schema, budgets and ordinary ledger validation the same.
Keep credential validation, ownership/job binding, signing lifecycle, expiry, revocation,
idempotency and obligation duplicate controls identical; A/B bypass only the exact payment
tuple comparison with mandate/grant scope. B→C isolates scope
enforcement rather than the whole reliability design. Evaluate replay, revocation and signing
separately through deterministic suites. Isolate state per trial. The broad simulator must
accept an otherwise valid alternate synthetic destination to be a meaningful baseline. Never
expose it through the public demo or connect it to real payment credentials.

### Measures and denominators

- Automatic legitimate completion: correct payments / 120 eligible legitimate trials per
  configuration, target ≥90%. Report unnecessary blocking separately, target ≤10%.
  Review-required cases are excluded from both automatic-workflow denominators.
- Review handling: report the 30 review trials separately. Changed/expired authority cannot
  execute before fresh confirmation; rejection permits no new payment. Report successful
  post-confirmation recovery and timeouts separately. C must satisfy every defined review
  safety assertion. Required review is not an unnecessary block.
- Attacks: unauthorized proposals and unauthorized transfers are distinct measures over
  90 attack trials per configuration. Judge authority against the frozen mandate. A correct-scope
  proposal on poisoned input is not an unauthorized transfer. Report model refusals without
  inventing gateway decisions.
- Document utility: period/amount accuracy, checked evidence support, conflict detection and
  use of supplied history against fixture truth. State denominators, include missing/ambiguous
  outputs and never count unavailable evidence as verified.
- Latency, calls/tokens, receipt delay and cost per workflow. Do not infer time savings from
  synthetic accuracy alone.

Direct gateway tests do not depend on persuading the model to misbehave and remain separate
from live-model results. Optional 3-5 exploratory usability sessions may compare manual and
assisted notice checking; report sample size, task accuracy and time. They are not a release
prerequisite or evidence of broad population impact.

### Release gates

Zero out-of-scope transfers in the deterministic suite. Zero duplicates in the race suite.
One failure blocks release. Mandate/grant binding, signing recovery, required review safety and
pinned-key verification must also pass. Store raw synthetic requests, expected verdicts, outcomes, fixture
version and release commit.

A model extraction issue may be disclosed with a smaller supported input set, provided the
gateway still holds and the narrowed evaluation is labelled honestly. If signing or verification
is incomplete, report a partial prototype and remove verified-receipt claims; it does not satisfy
this version's definition of done. Do not quietly turn mandatory evidence into an optional feature.

---

## 14. Deployment

**Stack:** React + TypeScript on Firebase Hosting · Firebase Auth · Python FastAPI on Cloud Run ·
Gemini via the Google Gen AI SDK · Firestore · Cloud Storage · Cloud KMS · Cloud Logging ·
Cloud Scheduler for the authority-signature and receipt drains.

**Sequence:**

1. Dedicated demo project, billing alerts, agreed spend ceiling, least-privilege identities.
   Pick a region supported by all services **and** the pinned model.
2. Provision keys, database, storage, identities with Terraform or an equivalent reviewed script.
3. Run schema, authorisation, concurrency and receipt fixtures in CI. Build containers. Deploy
   private backends before the frontend.
4. Seed synthetic tenants, landlords, balances and scenario documents.
5. Deploy the frontend. Test the deployed workflow. Record the release commit, model ID, prompt
   version and policy version.

**Observe:** job latency, model calls and tokens, gateway decisions by reason, duplicate
suppression, transaction conflicts, receipt backlog, signing failures, cost per workflow.
Correlation IDs across services.

### Fresh-browser demo access and interface

"Try demo" creates a Firebase anonymous-auth session and sends its verified ID token to the
authority. The server seeds a quota-limited scenario owned by that UID. No previous account
is needed; backend authentication and ownership checks remain mandatory. Accounts, documents,
obligations and jobs are isolated per session. Cross-session access must fail. The authority
reserves a new scenario ID and invokes the gateway's authority-only, quota-limited seed endpoint
for initial balances. The gateway remains the only runtime balance writer. Repeated seeding
of the same scenario returns its existing result and cannot replenish balances.

Show "Simulation — no real money" throughout. Reset is an authenticated owned operation:
create a fresh scenario/obligation, retain old paid records and never make a paid obligation
unpaid. It cannot alter an in-flight scenario or another user's state. Apply per-session and
aggregate quotas/rate limits. Keep evaluation baselines and administrative resets private.

Six views: dashboard, approval, agent activity, result, receipt trail and account-change review.
Support keyboard navigation, visible focus and text status labels. Make destination details
inspectable before approval and mask routine history. Signature metadata can appear in an
advanced view; bearer grants, sessions and signing secrets must never appear in the browser UI.

Billing alerts alone do not stop charges. Use max instance counts, bounded model calls and
per-user quotas.

---

## 15. Definition of done

- [ ] Normal payment completes end to end through the deployed system
- [ ] A changed destination cannot transfer money
- [ ] Repeated and simultaneous requests cannot pay twice
- [ ] Expiry and revocation behave as specified
- [ ] A legitimate account change requires a new approval
- [ ] The gateway persists evidence in the same transaction as the transfer
- [ ] Mandate/grant signing failures cannot activate authority; state recovers after restart
- [ ] In-app receipts verify against independently pinned keys, including supporting-record digests
- [ ] Pending states are displayed accurately, never as confirmed failures
- [ ] Gemini performs visible document work with checkable evidence
- [ ] Published results match saved A/B/C trials with separate automatic/review/attack denominators
- [ ] Fresh-browser demo entry creates isolated authenticated state; cross-session access and unsafe reset fail
