import type { Job, OrchestrationResult, PaymentFields, Screening } from "../api";
import { CardHead, D } from "../components/Details";
import { Check, Cross, Gate, Radar } from "../components/Icons";
import { Pipeline } from "../components/Pipeline";
import { StatusPill } from "../components/StatusPill";
import { formatMinor } from "../format";

const OUTCOME_COPY: Record<string, { title: string; tone: "ok" | "warn" | "danger"; body: string }> = {
  paid: { title: "Payment completed", tone: "ok", body: "The exact approved payment was made, once." },
  blocked: {
    title: "Blocked — outside what you approved",
    tone: "danger",
    body: "The agent proposed a payment that doesn't match your approval. The gateway refused it; no money moved.",
  },
  review_required: {
    title: "Needs your review",
    tone: "warn",
    body: "Something in the notice didn't match your approval, or was missing. Nothing was paid automatically.",
  },
  no_active_authority: {
    title: "Stopped — this approval is no longer active",
    tone: "warn",
    body: "The grant was revoked, superseded or expired, so the agent was not run. Approve (or renew) to continue.",
  },
  schema_invalid: {
    title: "The agent's answer couldn't be understood",
    tone: "danger",
    body: "Its output failed validation twice, so it was discarded. Nothing was paid.",
  },
  budget_exceeded: { title: "Stopped — task budget exceeded", tone: "warn", body: "Nothing was paid." },
  tool_error: {
    title: "Stopped — a document couldn't be read",
    tone: "warn",
    body: "The agent could not access an assigned document. Nothing was paid.",
  },
};

const FIELD_LABELS: Record<keyof PaymentFields, string> = {
  debit_account_id: "From account",
  destination_account_id: "To account",
  destination_version: "Destination version",
  amount_minor: "Amount",
  currency: "Currency",
  payment_reference: "Reference",
};

const SOURCE_LABEL: Record<string, string> = {
  runtime: "PayGuard's checks",
  model: "the agent",
  screening: "Laya pre-screen",
};

function ScreeningPanel({ screening }: { screening: Screening }) {
  const worst = Object.values(screening.probabilities)
    .flatMap((q) => Object.values(q))
    .reduce((a, b) => Math.max(a, b), 0);
  return (
    <section className="subsection" aria-label="Decision-model pre-screen">
      <h3 style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <Radar size={20} /> Pre-screen
      </h3>
      {screening.unavailable ? (
        <p>
          <span className="status-pill neutral">Screen unavailable</span>{" "}
          <span className="muted small">
            ({screening.unavailable}). Nothing is less safe: the agent's checks and the gateway still applied.
          </span>
        </p>
      ) : (
        <p>
          <span className={`status-pill ${screening.flags.length ? "warn" : "ok"}`}>
            {screening.flags.length ? "Flagged for review" : "No payee change detected"}
          </span>{" "}
          <span className="muted small">
            highest “redirects payment” probability {worst.toFixed(2)} · {(screening.elapsed_ms / 1000).toFixed(1)}s
          </span>
        </p>
      )}
      <p className="muted small">
        A fast decision model ({screening.model_id}) reads the notice before the agent does. It can only ever send a
        payment to review — it can never approve one or clear a check, so a miss here changes nothing.
      </p>
    </section>
  );
}

function show(field: keyof PaymentFields, fields: PaymentFields): string {
  const value = fields[field];
  return field === "amount_minor" ? formatMinor(value as number, fields.currency) : String(value);
}

function ApprovedVersusAttempted({ approved, attempted }: { approved: PaymentFields; attempted: PaymentFields }) {
  const keys = Object.keys(FIELD_LABELS) as (keyof PaymentFields)[];
  return (
    <>
      <h3>Approved vs. attempted</h3>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">You approved</th>
              <th scope="col">The agent attempted</th>
              <th scope="col">Match</th>
            </tr>
          </thead>
          <tbody>
            {keys.map((k) => {
              const same = approved[k] === attempted[k];
              return (
                <tr key={k} className={same ? undefined : "mismatch"}>
                  <th scope="row">{FIELD_LABELS[k]}</th>
                  <td>{show(k, approved)}</td>
                  <td>{show(k, attempted)}</td>
                  <td>{same ? "✓ same" : "✗ different"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="muted small">
        Both columns come from trusted records: your signed approval, and the attempt exactly as the gateway received
        it. The attempted value is preserved as evidence — never quietly corrected.
      </p>
    </>
  );
}

export function ResultView({
  result,
  pending,
  busy,
  onReplay,
  onViewReceipts,
  onCheckAgain,
  onGoReview,
  canReview,
}: {
  result: OrchestrationResult | null;
  pending: { jobId: string; authoritative: Job | null } | null;
  busy: boolean;
  onReplay: () => void;
  onViewReceipts: () => void;
  onCheckAgain: () => void;
  onGoReview: () => void;
  canReview: boolean;
}) {
  if (pending && !result) {
    const auth = pending.authoritative;
    return (
      <div className="card">
        <CardHead icon={<Gate />} title="Result" aside={<span className="status-pill warn">Outcome not yet known</span>} />
        <p role="status">
          We didn't get a response from the server. <strong>This is not a failure</strong> — the payment may or may not
          have gone through. Checking the authoritative status instead of guessing…
        </p>
        {auth && (
          <dl className="details">
            <D label="Job">
              <StatusPill status={auth.status} />
            </D>
            <D label="Obligation">
              <StatusPill status={auth.obligation_state} />
            </D>
            {auth.payment_id && (
              <D label="Payment" mono>
                {auth.payment_id}
              </D>
            )}
          </dl>
        )}
        <button onClick={onCheckAgain} disabled={busy}>
          {busy ? "Checking…" : "Check again"}
        </button>
      </div>
    );
  }
  if (!result) return null;

  const copy = OUTCOME_COPY[result.outcome] ?? { title: result.outcome, tone: "warn" as const, body: "" };
  const showComparison = result.approved && result.attempted && result.outcome !== "paid";

  return (
    <div className="card">
      <Pipeline
        states={
          result.outcome === "paid"
            ? { approve: "done", grant: "done", agent: "done", gateway: "done", receipt: "done" }
            : result.outcome === "blocked"
              ? { approve: "done", grant: "done", agent: "done", gateway: "stop", receipt: "done" }
              : { approve: "done", grant: "done", agent: "stop" }
        }
      />
      <CardHead
        icon={result.outcome === "paid" ? <Check /> : <Cross />}
        tone={copy.tone === "ok" ? "ok" : "danger"}
        eyebrow="Step 3 · the gateway decides"
        title="Result"
      />
      <p>
        <span className={`status-pill big ${copy.tone}`}>{copy.title}</span>
      </p>
      {copy.body && <p>{copy.body}</p>}

      {result.payment_id && (
        <p>
          Payment ID: <code>{result.payment_id}</code>
        </p>
      )}

      {result.reason_codes.length > 0 && (
        <p>
          Gateway reason:{" "}
          {result.reason_codes.map((r) => (
            <code key={r} className="reason">
              {r}
            </code>
          ))}
        </p>
      )}

      {showComparison && <ApprovedVersusAttempted approved={result.approved!} attempted={result.attempted!} />}

      {result.conflicts.length > 0 && (
        <>
          <h3>What didn't match</h3>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th scope="col">Field</th>
                  <th scope="col">Expected</th>
                  <th scope="col">Claimed</th>
                  <th scope="col">Detail</th>
                  <th scope="col">Found by</th>
                </tr>
              </thead>
              <tbody>
                {result.conflicts.map((c, i) => (
                  <tr key={i}>
                    <td>{c.field}</td>
                    <td>{c.expected}</td>
                    <td>{c.claimed}</td>
                    <td>{c.detail}</td>
                    <td>{SOURCE_LABEL[c.source] ?? c.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {result.screening && <ScreeningPanel screening={result.screening} />}

      {(result.outcome === "blocked" || result.outcome === "review_required") && canReview && (
        <div className="notice-box">
          <p>
            <strong>Don't act on the notice itself.</strong> If your landlord really changed banks, confirm it with them
            through a channel you already trust — not a number or link from this notice — and then review the change.
          </p>
          {/* Deliberately not a primary button: right after a suspicious notice,
              the UI must not nudge anyone toward changing the payee. */}
          <button onClick={onGoReview} disabled={busy}>
            I've confirmed a genuine account change
          </button>
        </div>
      )}

      <div className="actions">
        {result.outcome === "paid" && (
          <button onClick={onReplay} disabled={busy}>
            {busy ? "Replaying…" : "Replay this job"}
          </button>
        )}
        <button onClick={onViewReceipts} disabled={busy}>
          View receipt trail
        </button>
      </div>
      {result.outcome === "paid" && (
        <p className="muted small">Replaying never creates a second transfer for the same obligation.</p>
      )}
    </div>
  );
}
