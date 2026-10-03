import { useId, useState } from "react";
import type { Mandate } from "../api";
import { CardHead, D } from "../components/Details";
import { Swap } from "../components/Icons";
import { formatMinor } from "../format";

export function AccountChangeReview({
  mandate,
  busy,
  onSubmit,
  onRenew,
  onRevoke,
}: {
  mandate: Mandate;
  busy: boolean;
  onSubmit: (newAccountNumber: string, newLabel: string) => void;
  onRenew: () => void;
  onRevoke: () => void;
}) {
  const [newAccountNumber, setNewAccountNumber] = useState("");
  const [newLabel, setNewLabel] = useState("");
  const [confirming, setConfirming] = useState(false);
  const accountFieldId = useId();
  const labelFieldId = useId();
  const current = mandate.destination;
  const ready = newAccountNumber.trim() !== "" && newLabel.trim() !== "";

  return (
    <div className="card">
      <CardHead icon={<Swap />} eyebrow="Explicit confirmation only" title="Review a change">
        Only you can change where rent goes. Nothing is ever changed automatically from a document's text. Every
        account change needs this explicit confirmation, and the old approval stops working the moment you confirm.
      </CardHead>

      <div className="compare">
        <section>
          <h3>Currently approved</h3>
          <dl className="details">
            <D label="Pays to">{current?.label ?? mandate.destination_account_id}</D>
            <D label="Account" mono>
              {current?.account_number ?? "—"}
            </D>
            <D label="Version">{mandate.destination_version}</D>
          </dl>
        </section>
        <section>
          <h3>Proposed</h3>
          <dl className="details">
            <D label="Pays to">{newLabel.trim() || "—"}</D>
            <D label="Account" mono>
              {newAccountNumber.trim() || "—"}
            </D>
            <D label="Version">new</D>
          </dl>
        </section>
      </div>
      <p className="muted small">
        Unchanged either way: {formatMinor(mandate.amount_minor, mandate.currency)}, reference{" "}
        <code>{mandate.payment_reference}</code>, the same obligation. PayGuard can't verify who owns a new account —
        only confirm it if you got it from your landlord through a channel you trust.
      </p>

      <div className="field">
        <label htmlFor={accountFieldId}>New account number</label>
        <input
          id={accountFieldId}
          value={newAccountNumber}
          onChange={(e) => {
            setNewAccountNumber(e.target.value);
            setConfirming(false);
          }}
          autoComplete="off"
        />
      </div>
      <div className="field">
        <label htmlFor={labelFieldId}>Account holder / label</label>
        <input
          id={labelFieldId}
          value={newLabel}
          onChange={(e) => {
            setNewLabel(e.target.value);
            setConfirming(false);
          }}
          autoComplete="off"
        />
      </div>

      {!confirming ? (
        <button className="primary" onClick={() => setConfirming(true)} disabled={busy || !ready}>
          Review this change
        </button>
      ) : (
        <div className="notice-box" role="alert">
          <p>
            Confirm: future rent for this obligation goes to <strong>{newLabel.trim()}</strong> (
            <code>{newAccountNumber.trim()}</code>) instead of <strong>{current?.label}</strong>.
          </p>
          <button className="primary" onClick={() => onSubmit(newAccountNumber.trim(), newLabel.trim())} disabled={busy}>
            {busy ? "Confirming…" : "Yes, confirm the account change"}
          </button>{" "}
          <button onClick={() => setConfirming(false)} disabled={busy}>
            Go back
          </button>
        </div>
      )}

      <section className="subsection">
        <h3>Other options</h3>
        <div className="actions">
          <button onClick={onRenew} disabled={busy}>
            Renew an expired approval (same details)
          </button>
          <button className="danger" onClick={onRevoke} disabled={busy}>
            Revoke the current approval
          </button>
        </div>
      </section>
    </div>
  );
}
