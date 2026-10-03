type Tone = "ok" | "warn" | "danger" | "neutral";

const TONE_BY_STATUS: Record<string, Tone> = {
  PAID: "ok",
  COMMITTED: "ok",
  SIGNED: "ok",
  ACTIVE: "ok",
  VERIFIED: "ok",
  DONE: "ok",
  READY: "ok",
  UNPAID: "neutral",
  PENDING: "warn",
  EXPIRED: "warn",
  PENDING_SIGNATURE: "warn",
  WAITING_SIGNATURE: "warn",
  RUNNING: "warn",
  REVIEW_REQUIRED: "warn",
  MISSING: "warn",
  BLOCKED: "danger",
  REVOKED: "danger",
  CONSUMED: "neutral",
  SUPERSEDED: "neutral",
  FAILED: "danger",
  DIGEST_MISMATCH: "danger",
  SIGNATURE_INVALID: "danger",
  CHAIN_BROKEN: "danger",
  TAMPERED_SUPPORTING_RECORD: "danger",
};

/**
 * A status is always shown as a plain-text label alongside its color, never
 * color alone (SPEC.md Section 14: "text status labels").
 */
export function StatusPill({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="status-pill neutral">UNKNOWN</span>;
  const tone = TONE_BY_STATUS[status] ?? "neutral";
  return <span className={`status-pill ${tone}`}>{status.replaceAll("_", " ")}</span>;
}
