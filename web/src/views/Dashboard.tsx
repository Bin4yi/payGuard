import { useId, useState } from "react";
import type { Job, Mandate, Scenario } from "../api";
import { CardHead, D } from "../components/Details";
import { Home } from "../components/Icons";
import { Pipeline, type StepId, type StepState } from "../components/Pipeline";
import { StatusPill } from "../components/StatusPill";
import { formatMinor, parseMajorToMinor } from "../format";

function progress(scenario: Scenario, mandate: Mandate | null, job: Job | null): Partial<Record<StepId, StepState>> {
  const paid = scenario.obligation_state === "PAID";
  const stopped = job?.status === "REVIEW_REQUIRED" || job?.status === "FAILED";
  return {
    approve: mandate && mandate.signing_state === "SIGNED" ? "done" : "todo",
    grant: job?.grant_state === "ACTIVE" || job?.grant_state === "CONSUMED" || paid ? "done" : "todo",
    agent: paid ? "done" : stopped ? "stop" : "todo",
    gateway: paid ? "done" : "todo",
    receipt: paid ? "done" : "todo",
  };
}

export function Dashboard({
  scenario,
  mandate,
  job,
  busy,
  onReset,
  onRevoke,
  onUpdateAmount,
  onGoApprove,
  onGoActivity,
}: {
  scenario: Scenario;
  mandate: Mandate | null;
  job: Job | null;
  busy: boolean;
  onReset: () => void;
  onRevoke: () => void;
  onUpdateAmount: (amountMinor: number) => void;
  onGoApprove: () => void;
  onGoActivity: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [amountText, setAmountText] = useState("");
  const amountId = useId();
  const paid = scenario.obligation_state === "PAID";
  const approvalActive = mandate?.state === "APPROVED" && !paid;
  const parsedAmount = parseMajorToMinor(amountText);

  return (
    <>
      <Pipeline states={progress(scenario, mandate, job)} />
      <div className="card">
        <CardHead
          icon={<Home />}
          eyebrow="This month's rent"
          title="Dashboard"
          aside={<StatusPill status={scenario.obligation_state} />}
        >
          One obligation. It can be paid at most once, ever.
        </CardHead>

        <div className="well" style={{ marginBottom: "1rem" }}>
          <span className="eyebrow" style={{ color: "var(--text-soft)" }}>
            Rent instruction{scenario.instruction_version != null && ` · version ${scenario.instruction_version}`}
          </span>
          <div className="amount-hero">{formatMinor(scenario.amount_minor, scenario.currency)}</div>
          <span className="muted">
            to {scenario.destination_label ?? "—"}
            {scenario.destination_account_number && ` · ${scenario.destination_account_number}`}
          </span>
        </div>

        <dl className="details">
          <D label="Obligation" mono>
            {scenario.obligation_id}
          </D>
          <D label="Tenant balance">
            {scenario.tenant_balance_minor != null ? formatMinor(scenario.tenant_balance_minor, scenario.currency) : "—"}
          </D>
          {paid && scenario.payment_id && (
            <D label="Payment" mono>
              {scenario.payment_id}
            </D>
          )}
          <D label="Current approval">
            {mandate ? (
              <>
                <StatusPill status={mandate.state} /> <StatusPill status={mandate.signing_state} />
                {job && (
                  <>
                    {" "}
                    <StatusPill status={job.status} />
                  </>
                )}
              </>
            ) : (
              <span className="muted">No active approval</span>
            )}
          </D>
        </dl>

        <div className="actions">
          {!paid && !mandate && (
            <button className="primary" onClick={onGoApprove} disabled={busy}>
              Review and approve this payment
            </button>
          )}
          {approvalActive && job && (
            <button className="primary" onClick={onGoActivity} disabled={busy}>
              Go to agent activity
            </button>
          )}
          {approvalActive && (
            <button className="danger" onClick={onRevoke} disabled={busy}>
              Revoke approval
            </button>
          )}
          <button onClick={onReset} disabled={busy}>
            Start a fresh scenario
          </button>
          {!paid && !approvalActive && !editing && (
            <button onClick={() => setEditing(true)} disabled={busy}>
              Change the amount
            </button>
          )}
        </div>

        {!paid && !approvalActive && editing && (
          <form
            className="subsection"
            onSubmit={(e) => {
              e.preventDefault();
              if (parsedAmount != null) {
                onUpdateAmount(parsedAmount);
                setEditing(false);
                setAmountText("");
              }
            }}
          >
            <div className="field">
              <label htmlFor={amountId}>New monthly amount ({scenario.currency})</label>
              <input
                id={amountId}
                inputMode="decimal"
                value={amountText}
                onChange={(e) => setAmountText(e.target.value)}
                placeholder="e.g. 42,500.00"
                aria-invalid={amountText !== "" && parsedAmount == null}
                aria-describedby={`${amountId}-hint`}
              />
              <p id={`${amountId}-hint`} className="muted small">
                {amountText !== "" && parsedAmount == null
                  ? "Enter an exact positive amount with at most two decimals."
                  : "Changes apply to new approvals only. An existing approval is never edited."}
              </p>
            </div>
            <div className="actions">
              <button type="submit" className="primary" disabled={busy || parsedAmount == null}>
                Save instruction
              </button>
              <button type="button" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </div>
          </form>
        )}

        <p className="muted small">
          Starting a fresh scenario never erases or un-pays a previous one. Old paid history stays exactly as it was.
        </p>
      </div>
    </>
  );
}
