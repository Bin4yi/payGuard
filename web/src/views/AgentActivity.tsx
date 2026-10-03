import { useEffect, useState } from "react";
import type { Job, Mandate } from "../api";
import { CardHead, D } from "../components/Details";
import { Bot } from "../components/Icons";
import { StatusPill } from "../components/StatusPill";
import { secondsUntil } from "../format";

export function AgentActivity({
  job,
  mandate,
  busy,
  onRun,
  onRenew,
}: {
  job: Job;
  mandate: Mandate | null;
  busy: boolean;
  onRun: () => void;
  onRenew: () => void;
}) {
  const [remaining, setRemaining] = useState(() => secondsUntil(job.grant_expires_at));
  useEffect(() => {
    setRemaining(secondsUntil(job.grant_expires_at));
    const timer = window.setInterval(() => setRemaining(secondsUntil(job.grant_expires_at)), 1000);
    return () => window.clearInterval(timer);
  }, [job.grant_expires_at]);

  const paid = job.obligation_state === "PAID";
  const grantActive = job.grant_state === "ACTIVE";
  const expired = grantActive && remaining === 0;
  // A job that stopped for review (or failed) never re-runs by itself: the
  // way forward is a human decision — review a change, renew, or revoke.
  const stopped = job.status === "REVIEW_REQUIRED" || job.status === "FAILED";
  const canRun = grantActive && !expired && !paid && !stopped;

  return (
    <div className="card">
      <CardHead icon={<Bot />} eyebrow="Step 2 · a narrow, short-lived grant" title="Agent activity" aside={<StatusPill status={job.status} />} />
      <div className="well" style={{ marginBottom: "1rem", textAlign: "center" }}>
        <span className="eyebrow" style={{ color: "var(--text-soft)" }}>
          Grant
        </span>
        <div className="amount-hero" aria-live="off">
          {grantActive && !expired && remaining != null && !paid
            ? `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`
            : paid
              ? "Used"
              : "—"}
        </div>
        <StatusPill status={expired ? "EXPIRED" : job.grant_state} />
        {grantActive && !expired && remaining != null && !paid && (
          <span className="muted small"> usable for another {remaining}s, for this one payment only</span>
        )}
      </div>
      <dl className="details">
        <D label="Job" mono>
          {job.job_id}
        </D>
        <D label="Documents in scope" mono>
          {job.allowed_documents.length === 0 ? "none" : job.allowed_documents.join(", ")}
        </D>
      </dl>

      <p>
        The agent can only read the documents assigned to this job and <em>propose</em> a payment. It never sees a
        signing key or its own credential, and it cannot widen what you approved — the gateway independently re-checks
        the exact payment no matter what the agent proposes.
      </p>

      {paid ? (
        <p className="notice-box" role="status">
          This obligation is already paid. Running again is a safe replay: it returns the recorded result and can never
          pay twice.
        </p>
      ) : stopped ? (
        <div className="notice-box" role="status">
          <p>
            This job stopped {job.status === "FAILED" ? "with an error" : "for your review"} and won't run again on its
            own. Nothing was paid. From here you can review a genuine account change (after confirming it with your
            landlord directly), or revoke the approval from the dashboard and approve again with a notice you trust.
          </p>
        </div>
      ) : expired ? (
        <div className="notice-box" role="status">
          <p>
            This grant has expired. Expired authority is never silently extended — renew the approval (same payment
            details, a fresh confirmation) to continue.
          </p>
          <button className="primary" onClick={onRenew} disabled={busy}>
            Renew approval
          </button>
        </div>
      ) : !grantActive ? (
        <p className="notice-box" role="status">
          This job's grant is <strong>{job.grant_state ?? "missing"}</strong>, so the agent will not run. Approve again from
          the dashboard.
        </p>
      ) : null}

      <button className="primary big" onClick={onRun} disabled={busy || !(canRun || paid)}>
        {busy ? "Agent working…" : paid ? "Replay this job" : "Run the agent now"}
      </button>

      {mandate && (
        <details className="advanced">
          <summary>Advanced: signature metadata</summary>
          <dl className="details">
            <D label="Mandate" mono>
              {mandate.mandate_id} (v{mandate.version})
            </D>
            <D label="Signing">
              <StatusPill status={mandate.signing_state} />
            </D>
            <D label="Signed payload digest" mono>
              {mandate.digest ?? "—"}
            </D>
            <D label="Key id" mono>
              {mandate.key_id ?? "—"}
            </D>
            <D label="Destination version">{mandate.destination_version}</D>
          </dl>
          <p className="muted small">
            Bearer grants, session tokens and signing secrets are never shown here or anywhere in this UI.
          </p>
        </details>
      )}
    </div>
  );
}
