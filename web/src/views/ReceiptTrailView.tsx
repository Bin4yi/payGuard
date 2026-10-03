import type { ExportResult, ReceiptTrail } from "../api";
import { CardHead, D } from "../components/Details";
import { Receipt } from "../components/Icons";
import { StatusPill } from "../components/StatusPill";

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

export function ReceiptTrailView({
  trail,
  exported,
  busy,
  onExport,
}: {
  trail: ReceiptTrail;
  exported: ExportResult | null;
  busy: boolean;
  onExport: () => void;
}) {
  const v = exported?.verification;

  return (
    <div className="card">
      <CardHead icon={<Receipt />} eyebrow="Step 4 · tamper-evident evidence" title="Receipt trail" />
      <p>
        {trail.gaps.length > 0 ? (
          <span className="status-pill danger">Receipts missing from the middle: #{trail.gaps.join(", #")}</span>
        ) : trail.completeness_unknown ? (
          <span className="status-pill warn">Completeness unknown</span>
        ) : (
          <span className="status-pill ok">All events verified, chain complete</span>
        )}
      </p>
      <p className="muted small">
        "Signed" means a signature is stored. "Verified" means this page just checked it against independently
        configured trusted keys. A missing record in the middle is flagged as such; a not-yet-signed record at the end
        is only "completeness unknown" — never silently upgraded to "complete".
      </p>

      {trail.entries.length === 0 ? (
        <p>No signed receipts yet.</p>
      ) : (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th scope="col">Sequence</th>
                <th scope="col">Status</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {trail.entries.map((e) => (
                <tr key={e.sequence}>
                  <td>{e.sequence}</td>
                  <td>
                    <StatusPill status={e.status} />
                  </td>
                  <td>{e.detail ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <section className="subsection" aria-label="Evidence export">
        <h3>Export evidence</h3>
        <p className="muted small">
          A portable bundle of the signed receipts, the signed approval they reference, and the exact attempted payments.
          It carries no keys and no credentials — whoever checks it must bring their own trusted keys.
        </p>
        <button onClick={onExport} disabled={busy || trail.entries.length === 0}>
          {busy ? "Preparing…" : "Build and verify export"}
        </button>

        {v && exported && (
          <div className="subsection">
            <p>
              {v.all_verified && !v.completeness_unknown ? (
                <span className="status-pill ok">Bundle verified independently</span>
              ) : v.all_verified ? (
                <span className="status-pill warn">Verified, completeness unknown</span>
              ) : (
                <span className="status-pill danger">Bundle failed verification</span>
              )}
            </p>
            <dl className="details">
              <D label="Signed approval">
                <StatusPill status={v.mandate_status} />
              </D>
              <D label="Receipts">
                {v.receipts.filter((r) => r.status === "VERIFIED").length} of {v.receipts.length} verified
              </D>
              <D label="Supporting records">
                {v.attempt_failures.length === 0 ? "all digests recomputed and match" : v.attempt_failures.join("; ")}
              </D>
            </dl>
            <button style={{ marginTop: "1rem" }} onClick={() => downloadJson(`payguard-evidence-${trail.workflow_id}.json`, exported.bundle)}>
              Download bundle (.json)
            </button>
          </div>
        )}
      </section>
    </div>
  );
}
