# Claude Code — PayGuard Build Prompt

**Version 2.1 · 13 September 2026**

Paste the block below into Claude Code in a repository containing the matching version 2.1
`PROJECT.md` and `SPEC.md`. This prompt implements the frozen MVP; do not add optional protocols
or features before the release gates pass.

## The prompt

You are building PayGuard, a security-critical synthetic-payment prototype. Read PROJECT.md
and SPEC.md completely before writing code. SPEC.md is the implementation contract. Follow
explicit user changes and update affected contracts consistently; surface any unresolved
security conflict before implementing it. Never silently weaken a required invariant.

### What this system is

Gemini reads a rent notice, extracts evidence, checks supplied history and proposes a payment.
A separate deterministic gateway decides whether the actual payment matches the user's
approved authority. The authority signs its record of user confirmation; this is not a
user-held-key signature. The runner's output is a proposal, never authorization.

PayGuard is AP2-inspired and uses private mandate, grant and receipt JWS profiles. AP2 already
specifies verifier authorization, constraints and signed acceptance/rejection receipts.
Do not claim AP2 lacks these mechanisms, that our profiles adopt AP2 directly, or that
interoperability has been demonstrated. The contribution is implemented and measured behaviour.

### Non-negotiable rules

1. The runner has no Firestore permission, KMS permission or alternate payment path. The
   browser uses owned authority APIs, never direct Firestore access.
2. Never call Gemini, KMS or another external API from a Firestore transaction callback.
   Transactional reads/writes are expected; signing and inference must happen outside.
3. Transport money as strict integers in minor units. Reject floats, strings, booleans,
   negatives, zero and values over the demo ceiling. Do not coerce malformed input.
4. Reject unknown request fields and duplicate JSON keys before normal parsing loses them.
   Idempotency-Key exists only in its header, not also in the body.
5. Preserve the actual proposal. Never silently correct a wrong destination and claim a block.
6. No bearer grants, signing secrets or model credentials enter browser UI, prompts or logs.
   Keep personal documents and raw sensitive data out of operational logs. Synthetic attempt
   tuples belong in access-controlled evidence, not general logs.
7. Implement and verify the deterministic security boundary before connecting Gemini.
8. Bind actual request, signed grant, stored grant, signed mandate, owner, job, obligation,
   runner and policy. Check online state within the transaction on every retry.
9. One obligation means at most one transfer across jobs, keys and replacement grants.
   A consumed grant can only retrieve a matching existing result. No new execution.
10. Pending mandate/grant signing cannot create executable authority. Signing recovery
    retries frozen payloads; it never silently extends expiry or reactivates old scope.
11. Minimal account-change review and expiry recovery are required. Fresh user confirmation
    preserves the same unpaid obligation and invalidates the previous authority atomically.
12. In-app receipt verification uses independently configured trusted public keys. An
    exported key cannot establish its own trust. SIGNED, VERIFIED and completeness unknown
    are separate statements.

Implement the threat assumptions and S01-S07 in SPEC.md §4 explicitly. Trusted service code
and state remain assumptions; do not claim Firestore IAM provides collection-level isolation
between every trusted backend. Verify that the runner has no database permissions.

### Tech stack

- Backend: Python 3.12, FastAPI, one Cloud Run service per role
- Data: Firestore native mode; Cloud Storage for documents
- Crypto: Cloud KMS RSA-PSS SHA-256 (PS256), two keys:
  authority key for mandates AND grants with distinct types/validators;
  receipt key for the receipt worker only
- Model: Gemini through Google Gen AI SDK; verified model ID pinned in config
- Frontend: React + TypeScript + Vite, Firebase Hosting and Firebase Auth
- Tests: pytest and local Firestore emulator; real deployed checks reported separately
- Infrastructure: Terraform

### Repository layout

```
/services
  /authority       # confirmation, signature recovery, grants, jobs, user-facing reads
  /gateway         # payment enforcement and synthetic ledger writes
  /runner          # Gemini orchestration and narrow tools
  /receipts        # durable event signing
  /shared          # strict schemas, normalization, profile-specific JWS validation
/web
/infra
/fixtures
  /golden
  /scenarios       # development and demo fixtures
  /eval            # held-out final corpus
/tests
  /unit
  /security
  /concurrency
  /integration
/docs
```

## Phase 1 — Deterministic core

Local emulator only. No Gemini, frontend or cloud deployment.

1. Create strict schemas for signed mandate payload, grant claims, payment request and
   receipt event. Implement the versioned normalization in SPEC.md §9: sorted-key UTF-8
   JSON, no whitespace, ASCII identifiers, integer numeric values; digest input is the
   schema version, one zero byte and payload bytes. Never include a digest in its own input.
2. Add golden fixtures for canonical payload bytes, semantic payment digests and signature
   profiles. Separate the payment tuple digest from transport IDs; hash the full versioned attempt
   record separately so its exported context is also protected.
3. Implement SPEC.md §11 state and gateway §7. Use injected identity/crypto interfaces with
   test-only local adapters so transaction rules can be checked without KMS. Local adapters
   must not be selectable in production deployment. Phase 1 is not evidence of cloud auth.
4. Enforce the obligation lock and idempotency key independently. Complete all transaction
   reads before writes. Transfer, consumption, attempt and event outbox commit together.
5. Implement all reason codes in SPEC.md §7 and the read-only matching-retry branch.
6. Build state tests for active, pending, revoked, consumed, expired and superseded authority,
   job/mandate binding, replacement races and changed payload digests.

Phase 1 exit evidence:
- An authorized request pays once.
- Changed destination, amount, obligation or mandate/job binding cannot debit.
- 25 concurrent valid requests for one obligation yield exactly one transfer.
- Same key and tuple return the same transfer; changed tuple with the key conflicts.
- New key cannot create another transfer.
- Pending, expired, revoked or superseded authority cannot execute.
- Failed required outbox persistence aborts the transfer.
- Replacement confirmation and execution follow committed ordering.

## Phase 2 — Approval, signing and recovery

1. Implement SPEC.md §5 confirmation: frozen snapshot, owned identity, one-time nonce,
   recorded approval and separate PENDING signing state. Repeated confirm recovers status.
2. Implement payguard-mandate+jwt exactly, using KMS outside transactions and compare-and-set
   persistence. The gateway verifies its signature, fields and digest against the snapshot.
3. Implement SPEC.md §6 job/grant reservation, frozen token issuance time, one grant per
   approved mandate, activation rechecks and token-hash binding. No runner receives an
   unpersisted or inactive candidate token. No silent renewal after expiry.
4. Implement scheduled authority signature recovery and a best-effort immediate attempt.
   Test KMS failure, persistence loss, restart, duplicate worker and revoke-during-signing.
5. Implement revocation, consumed result lookup and account-change/expiry review APIs.
   A confirmed replacement atomically invalidates old authority and retains the obligation.
6. Implement ordered receipt signing with a separate key and compare-and-set chain head.
   Add in-app verification and exported supporting-record digest checks with pinned keys.
   Standalone verifier binaries, final checkpoints and anchoring remain optional.

Phase 2 exit evidence:
- Wrong signature/key/type/issuer/audience/principal and type confusion are denied.
- A grant cannot select another job, mandate, owner or obligation.
- Revocation-before-execution and execution-before-revocation follow committed order.
- No grant activates while mandate/grant signing is pending or has expired.
- Signing/persistence recovery survives restarts without duplicate authority or transfers.
- A reviewed change pays only after fresh approval and only while the obligation is unpaid.
- Receipt and supporting-record tampering is detected; truncation reports completeness
  unknown rather than claiming every event is present.

## Phase 3 — Gemini workflow

Connect Gemini only after the deterministic boundary passes.

1. Implement only the tools in SPEC.md §10. Bind every context/document/status lookup to
   the assigned job. Do not expose approval, review creation, new-payee creation or issuance.
2. The runner obtains only its assigned persisted grant from the private endpoint. Attach
   credentials outside model context. Strictly validate model output with one repair retry.
3. Resolve source IDs, pages/text spans and supporting quotes against actual job inputs.
   Unchecked citations remain assertions. Missing/ambiguous values require review.
4. Enforce document/page/size/call/time budgets. Pin model, SDK and prompt versions.
5. Show useful interpretation: period and amount extraction, supplied payment-history
   reconciliation, discrepancy reporting and explanation from authoritative result facts.
6. Enforce deterministic review flags in runtime code; model output cannot clear them.

Phase 3 exit evidence:
- A normal notice produces checked evidence and one correct payment.
- If the model attempts an unauthorized tuple, the gateway blocks it and records that tuple.
- If the model refuses before calling the gateway, record model activity; do not invent a
  gateway attempt. Show a separately labelled direct gateway test when demonstrating refusal.
- If poisoned input produces the approved tuple, do not mislabel a within-scope transfer
  as an authorization failure. Measure missed conflict detection separately.
- Ambiguity, changed account and expired grant follow the required review/reconfirmation path.

## Phase 4 — Frontend and isolated demo access

Implement the six views in SPEC.md §14: dashboard, approval, activity, result, receipt trail
and account-change review. Keep the latter minimal but functional and mandatory.

- Keyboard navigation, visible focus, readable contrast and textual outcomes.
- Display exact consequential details before approval. Show approved-versus-attempted
  details on refusal. Mask routine history; let the owner inspect the full details.
- Signing readiness, committed payment, receipt signing and verification are separate states.
  A timeout is an unknown/pending result until authoritative status resolves it.
- Show signature metadata in an advanced view, never bearer grants or session credentials.
- Try demo obtains Firebase anonymous authentication and seeds owned synthetic state.
  Backend ID-token and ownership checks remain required.
- Show Simulation — no real money throughout. A reset creates a fresh owned scenario and
  obligation; it cannot make an existing paid obligation unpaid, affect another session
  or mutate an in-flight scenario. Use the authority-only gateway seed endpoint for initial
  balances; repeated seeding cannot replenish them. Bound session and aggregate usage.

Phase 4 exit evidence:
A fresh browser can complete the normal, blocked, replay and review flows without a prior
account. A second session cannot read or modify the first session's data. Reset preserves
paid history and cannot defeat an obligation lock.

## Phase 5 — Cloud, controlled evaluation and submission

1. Provision service identities, two KMS keys and minimum permissions. Protect internal
   Cloud Run calls with verified audience and principal. Deploy backends before frontend.
   Provide authority/receipt drain schedules and bounded quotas; do not expose broad baselines.
2. Check real deployed negative access: runner cannot write Firestore, sign or invoke an
   alternate payment path. Record evidence and limitations, not claims of universal proof.
3. Build SPEC.md §13 evaluation: 40 automatic legitimate + 10 review-required + 30 poisoned
   workflows, three runs for each A/B/C configuration: 240 per configuration, 720 total.
4. A→B changes only the safety instruction. B→C changes only the payment boundary.
   Keep model, inputs/history, schemas and budgets fixed. Credential and ownership/job
   binding, signing lifecycle, expiry, revocation and duplicate controls remain identical.
   A/B bypass only the exact tuple-to-scope comparison; deterministic tests assess the
   shared replay, revocation and signing controls separately.
5. Keep dev fixtures separate. Freeze expected outcomes and scripted review responses before
   final trials. Save raw synthetic requests, expected/observed results, versions and commit.
6. Report unauthorized proposals and transfers separately; report automatic completion,
   required-review handling, evidence quality, history reconciliation, latency and cost with
   their actual denominators. No unmeasured time-saving or production-security claims.
7. Run the three-minute demo in PROJECT.md §8. Label direct gateway tests and actual model
   behaviour distinctly. Audit every item of SPEC.md §15 against saved evidence.

## Working process

Work through phases in order. At each phase report the files changed, actual checks run,
results and unresolved issues. Preserve the user's requested phase-review checkpoints; if
the user authorizes continuing through phases, continue without repeatedly asking permission.

Write meaningful security and concurrency tests before the corresponding implementation.
For missing details that change authorization, identify the ambiguity before making a
security-sensitive choice. Resolve routine implementation details using the agreed contract.

If a bypass is discovered, report it and fix the affected boundary before advancing. Keep
commits focused and labelled with the relevant specification section. Never mark a feature
complete from code inspection alone when its required execution evidence is missing.

## Follow-up prompts

### After Phase 1:

Expand the deterministic suite across SPEC.md §13. Assert each case's specified outcome.
Valid retries and races may return one existing transfer; they should not all assert zero
transfers. Cover signature and deployed identity cases after their Phase 2/5 dependencies.

### After Phase 3:

Build the frozen 80-workflow evaluation harness with the A/B/C controls and separate
automatic, review and attack denominators in SPEC.md §13. Save all trials and versions.
Do not tune prompts against the held-out final corpus.

### Before the demo:

Verify isolated anonymous-auth sessions, the persistent Simulation label and the owned
fresh-scenario reset. Confirm reset cannot erase a paid obligation or change in-flight state.

### Before submission:

Audit SPEC.md §15 against actual saved evidence. Report every incomplete item clearly.
Confirm the proposal uses AP2-inspired/private-profile wording and only measured results.
