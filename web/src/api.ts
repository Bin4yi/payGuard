// Thin client for the PayGuard demo backend (services/api/app.py).
//
// This talks only to the backend's *user* HTTP surface. It never holds a
// signing key or a grant token — only the session's own bearer id token,
// the ordinary credential a browser is expected to hold (a Firebase Auth ID
// token in the real deployment this stands in for). docs/SPEC.md Section 6:
// "Keep the token out of prompts, browser storage, document content and
// logs" refers to the *grant*; this file never sees one.

const BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "http://localhost:8000";

/** The server answered, and said no. The outcome is known. */
export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/**
 * The request never got a definite answer (offline, timeout, dropped
 * connection). The outcome is UNKNOWN — the server may well have acted.
 * SPEC.md F09: "A timeout never displays as a confirmed failure", so callers
 * must reconcile against authoritative status instead of reporting failure.
 */
export class NetworkError extends Error {}

export interface Session {
  uid: string;
  idToken: string;
}

export interface MandateRef {
  mandate_id: string;
  state: string;
  signing_state: string | null;
  job_id: string | null;
}

export interface Scenario {
  scenario_id: string;
  obligation_id: string;
  obligation_state: string | null;
  payment_id: string | null;
  amount_minor: number;
  currency: string;
  instruction_version: number | null;
  destination_label: string | null;
  destination_account_number: string | null;
  destination_version: number | null;
  tenant_balance_minor: number | null;
  current_mandate: MandateRef | null;
}

export interface DestinationView {
  version: number;
  label: string | null;
  account_number: string | null;
}

export interface Mandate {
  mandate_id: string;
  version: number;
  state: string;
  signing_state: string | null;
  obligation_id: string;
  debit_account_id: string;
  destination_account_id: string;
  destination_version: number;
  destination: DestinationView | null;
  amount_minor: number;
  currency: string;
  payment_reference: string;
  supersedes_mandate_id: string | null;
  issued_grant_id: string | null;
  digest: string | null;
  key_id: string | null;
  nonce?: string;
  review_kind?: "destination_change" | "expiry_recovery";
}

export interface Job {
  job_id: string;
  status: string;
  mandate_id: string;
  obligation_id: string;
  grant_id: string;
  grant_state: string | null;
  grant_expires_at: number | null;
  obligation_state: string | null;
  payment_id: string | null;
  allowed_documents: string[];
}

export interface Conflict {
  field: string;
  expected: string;
  claimed: string;
  detail: string;
  source: "model" | "runtime" | "screening";
}

export interface PaymentFields {
  debit_account_id: string;
  destination_account_id: string;
  destination_version: number;
  amount_minor: number;
  currency: string;
  payment_reference: string;
}

export type Outcome =
  | "paid"
  | "blocked"
  | "review_required"
  | "schema_invalid"
  | "budget_exceeded"
  | "no_active_authority"
  | "tool_error";

/** A decision-model pre-screen (Laya / Jev). Untrusted model output. */
export interface Screening {
  model_id: string;
  version: string;
  probabilities: Record<string, Record<string, number>>;
  flags: { document_id: string; question: string; probability: number; threshold: number; reason: string }[];
  unavailable: string | null;
  elapsed_ms: number;
}

export interface OrchestrationResult {
  outcome: Outcome;
  conflicts: Conflict[];
  gateway_status: string | null;
  reason_codes: string[];
  payment_id: string | null;
  approved: PaymentFields | null;
  attempted: PaymentFields | null;
  screening: Screening | null;
}

export interface ReceiptEntry {
  sequence: number;
  status: string;
  detail: string | null;
}

export interface ReceiptTrail {
  workflow_id: string;
  completeness_unknown: boolean;
  gaps: number[];
  entries: ReceiptEntry[];
}

export interface ExportResult {
  bundle: Record<string, unknown>;
  verification: {
    all_verified: boolean;
    completeness_unknown: boolean;
    mandate_status: string;
    gaps: number[];
    attempt_failures: string[];
    receipts: ReceiptEntry[];
  };
}

async function request<T>(session: Session | null, method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (session) headers["Authorization"] = `Bearer ${session.idToken}`;

  let resp: Response;
  try {
    resp = await fetch(`${BASE_URL}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new NetworkError("No response from the server — the outcome is not yet known.");
  }

  const data = await resp.json().catch(() => null);
  if (!resp.ok) {
    const detail = data?.detail;
    if (typeof detail === "object" && detail !== null && !Array.isArray(detail)) {
      throw new ApiError(resp.status, detail.code ?? "UNKNOWN", detail.message ?? resp.statusText);
    }
    // FastAPI's own request-validation errors arrive as a list.
    const first = Array.isArray(detail) ? detail[0]?.msg : undefined;
    throw new ApiError(resp.status, resp.status === 422 ? "INVALID_REQUEST" : "UNKNOWN", first ?? resp.statusText);
  }
  return data as T;
}

export async function createAnonymousSession(): Promise<Session> {
  const data = await request<{ uid: string; id_token: string }>(null, "POST", "/v1/demo/anonymous-session");
  return { uid: data.uid, idToken: data.id_token };
}

export const createScenario = (s: Session) => request<Scenario>(s, "POST", "/v1/demo/scenarios");
export const getCurrentScenario = (s: Session) => request<Scenario>(s, "GET", "/v1/demo/scenarios/current");

export const updateRentInstruction = (s: Session, amountMinor: number) =>
  request<{ version: number; amount_minor: number }>(s, "POST", "/v1/rent-instructions", { amount_minor: amountMinor });

/** The server assigns the document id — a client never chooses one. */
export const registerDocument = (s: Session, pages: string[], mediaType: DocumentMediaType = "text/plain") =>
  request<{ document_id: string; page_count: number }>(s, "POST", "/v1/documents", { pages, media_type: mediaType });

export type DocumentMediaType = "text/plain" | "image/png" | "image/jpeg" | "image/webp";

export const createMandate = (s: Session, documentIds: string[]) =>
  request<Mandate>(s, "POST", "/v1/mandates", { document_ids: documentIds });
export const confirmMandate = (s: Session, mandateId: string, version: number, nonce: string) =>
  request<Mandate>(s, "POST", `/v1/mandates/${mandateId}/confirm`, { version, nonce });
export const getMandate = (s: Session, mandateId: string) => request<Mandate>(s, "GET", `/v1/mandates/${mandateId}`);
export const revokeMandate = (s: Session, mandateId: string) =>
  request<Mandate>(s, "POST", `/v1/mandates/${mandateId}/revoke`);

/** Destination change: both fields. Nothing is approved until confirmed. */
export const reviewAccountChange = (s: Session, mandateId: string, newAccountNumber: string, newLabel: string) =>
  request<Mandate>(s, "POST", `/v1/mandates/${mandateId}/review`, {
    new_account_number: newAccountNumber,
    new_label: newLabel,
  });

/** Expiry recovery: identical payment details, a fresh approval. */
export const renewApproval = (s: Session, mandateId: string) =>
  request<Mandate>(s, "POST", `/v1/mandates/${mandateId}/review`, {});

export const createJob = (s: Session, mandateId: string) =>
  request<Job>(s, "POST", "/v1/jobs", { mandate_id: mandateId });
export const getJob = (s: Session, jobId: string) => request<Job>(s, "GET", `/v1/jobs/${jobId}`);
export const runJob = (s: Session, jobId: string) => request<OrchestrationResult>(s, "POST", `/v1/jobs/${jobId}/run`);
export const getJobReceipts = (s: Session, jobId: string) =>
  request<ReceiptTrail>(s, "GET", `/v1/jobs/${jobId}/receipts`);
export const exportReceipts = (s: Session, jobId: string) =>
  request<ExportResult>(s, "POST", `/v1/jobs/${jobId}/receipt-export`);

// ---- Attack Lab ----

export interface LabAttack {
  attack_id: string;
  title: string;
  technique: string;
  text: string;
  is_attack: boolean;
}

export interface LabLayer {
  layer_id: "laya" | "runtime" | "gateway";
  name: string;
  verdict: "flagged" | "passed" | "stopped" | "off" | "unavailable" | "not_reached";
  /** Did this layer do the right thing on its own? null = it didn't run. */
  good: boolean | null;
  detail: string;
  score?: number | null;
  threshold?: number | null;
  reason_codes?: string[];
  status?: string | null;
  conflicts?: { field: string; expected: string; claimed: string; detail: string }[];
}

export interface LabRun {
  attack: { attack_id: string; title: string; technique: string; is_attack: boolean };
  approved: { destination_label: string; account_number: string; amount_minor: number; currency: string };
  agent: {
    model_id: string;
    proposes_payment: boolean;
    destination_account_id: string | null;
    amount_minor: number | null;
    currency: string | null;
    redirected: boolean;
    wrong_amount: boolean;
  };
  layers: LabLayer[];
  money_to_attacker_minor: number;
  paid_to_landlord_minor: number;
  verdict: "stopped" | "breached" | "paid_correctly" | "not_paid";
}

export const listLabAttacks = (s: Session) =>
  request<{ attacks: LabAttack[] }>(s, "GET", "/v1/lab/attacks").then((r) => r.attacks);
export const runLabAttack = (s: Session, attackId: string) =>
  request<LabRun>(s, "POST", "/v1/lab/run", { attack_id: attackId });
export const runLabText = (s: Session, text: string) => request<LabRun>(s, "POST", "/v1/lab/run", { text });
