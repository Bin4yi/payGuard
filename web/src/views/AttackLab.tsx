import { useEffect, useId, useMemo, useState } from "react";
import { ApiError, listLabAttacks, runLabAttack, runLabText, type LabAttack, type LabLayer, type LabRun, type Session } from "../api";
import { Flask } from "../components/Icons";
import { PhotoNotice } from "../components/PhotoNotice";
import { formatMinor } from "../format";

const LAYER_ORDER: LabLayer["layer_id"][] = ["laya", "runtime", "gateway"];

const VERDICT_WORD: Record<LabLayer["verdict"], string> = {
  flagged: "Flagged",
  passed: "Passed",
  stopped: "Stopped",
  off: "Off",
  unavailable: "Unavailable",
  not_reached: "Nothing sent",
};

function LayerCell({ layer }: { layer: LabLayer | undefined }) {
  if (!layer) return <span className="layer-cell">–</span>;
  const tone = layer.good === null ? "" : layer.good ? " good" : " bad";
  const score = layer.layer_id === "laya" && layer.score != null ? ` ${layer.score.toFixed(2)}` : "";
  return (
    <span className={`layer-cell${tone}`} title={layer.detail}>
      {layer.good === true ? "✓ " : layer.good === false ? "✗ " : ""}
      {VERDICT_WORD[layer.verdict]}
      {score}
    </span>
  );
}

function RunDetail({ run, text }: { run: LabRun; text: string | null }) {
  const a = run.agent;
  const attackerTarget = a.redirected ? a.destination_account_id : null;
  return (
    <section className="card" aria-live="polite" aria-label="Attack result">
      <div className="card-head">
        <div style={{ flex: 1 }}>
          <span className="eyebrow">{run.attack.is_attack ? "Attack" : "Control"}</span>
          <h2>{run.attack.title}</h2>
          <p>{run.attack.technique}</p>
        </div>
        <span className={`status-pill big ${run.verdict === "stopped" || run.verdict === "paid_correctly" ? "ok" : "danger"}`}>
          {run.verdict === "stopped"
            ? "Stopped · attacker got LKR 0.00"
            : run.verdict === "paid_correctly"
              ? "Paid the real landlord, once"
              : run.verdict === "breached"
                ? "Money reached the attacker"
                : "Not paid"}
        </span>
      </div>

      {text && (
        <div className="well" style={{ marginBottom: "1rem" }}>
          <p className="notice-text">{text}</p>
        </div>
      )}

      <div className="compare">
        <section>
          <h3>You approved</h3>
          <p style={{ margin: 0 }}>
            <strong>{formatMinor(run.approved.amount_minor, run.approved.currency)}</strong> to {run.approved.destination_label}{" "}
            <code>{run.approved.account_number}</code>
          </p>
        </section>
        <section>
          <h3>The compromised agent tried</h3>
          {a.proposes_payment ? (
            <p style={{ margin: 0 }}>
              <strong className={a.wrong_amount ? "text-danger" : undefined}>
                {formatMinor(a.amount_minor!, a.currency ?? "LKR")}
              </strong>{" "}
              to <code>{attackerTarget ?? "the approved landlord"}</code>
            </p>
          ) : (
            <p style={{ margin: 0 }}>No payment: it couldn't find an amount, so it asked for clarification.</p>
          )}
        </section>
      </div>

      <h3>Each layer, tested on its own</h3>
      <div className="layers-detail">
        {LAYER_ORDER.map((id) => {
          const layer = run.layers.find((l) => l.layer_id === id)!;
          return (
            <article key={id} className="layer-card">
              <h4>
                {layer.name} <LayerCell layer={layer} />
              </h4>
              <p>{layer.detail}</p>
              {layer.reason_codes && layer.reason_codes.length > 0 && (
                <p>
                  {layer.reason_codes.map((r) => (
                    <code key={r} className="reason">
                      {r}
                    </code>
                  ))}
                </p>
              )}
              {layer.layer_id === "laya" && layer.score != null && (
                <p className="small">
                  “Redirects payment” probability {layer.score.toFixed(2)} (flags at {layer.threshold?.toFixed(2)})
                </p>
              )}
            </article>
          );
        })}
      </div>
      <p className="muted small">
        The gateway test is the worst case: the pre-screen and the review gate are skipped, and the compromised agent's
        exact request is sent with a valid signed grant. The attacker's account is a real, valid account, so a refusal
        can't be explained by it not existing. Agent: <code>{a.model_id}</code>, a deliberately obedient stand-in that
        follows every injected instruction.
      </p>
    </section>
  );
}

export function AttackLab({ session }: { session: Session }) {
  const [attacks, setAttacks] = useState<LabAttack[]>([]);
  const [runs, setRuns] = useState<Record<string, LabRun>>({});
  const [selected, setSelected] = useState<{ run: LabRun; text: string | null } | null>(null);
  const [running, setRunning] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [customText, setCustomText] = useState("");
  const customId = useId();

  useEffect(() => {
    listLabAttacks(session).then(setAttacks, (e) => setError(String(e)));
  }, [session]);

  const score = useMemo(() => {
    const done = Object.values(runs).filter((r) => r.attack.is_attack);
    return {
      tried: done.length,
      fooled: done.filter((r) => r.agent.redirected || r.agent.wrong_amount).length,
      stopped: done.filter((r) => r.verdict === "stopped").length,
      stolen: done.reduce((sum, r) => sum + r.money_to_attacker_minor, 0),
      layaCaught: done.filter((r) => r.layers.find((l) => l.layer_id === "laya")?.good === true).length,
    };
  }, [runs]);

  const runOne = async (attack: LabAttack) => {
    setRunning(attack.attack_id);
    setError(null);
    try {
      const run = await runLabAttack(session, attack.attack_id);
      setRuns((prev) => ({ ...prev, [attack.attack_id]: run }));
      setSelected({ run, text: attack.text });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(null);
    }
  };

  const runAll = async () => {
    for (const attack of attacks) {
      // eslint-disable-next-line no-await-in-loop
      await runOne(attack);
    }
  };

  const runCustom = async (text: string) => {
    setRunning("custom");
    setError(null);
    try {
      const run = await runLabText(session, text);
      setSelected({ run, text });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setRunning(null);
    }
  };

  return (
    <>
      <section className="card">
        <div className="card-head">
          <span className="icon-well danger">
            <Flask />
          </span>
          <div>
            <span className="eyebrow" style={{ color: "var(--danger)" }}>
              Red team
            </span>
            <h2>Attack Lab</h2>
            <p>
              Real scam techniques against an agent that obeys every one of them. Each defence layer is tested on its
              own, in a sealed sandbox that never touches your scenario.
            </p>
          </div>
        </div>

        <div className="lab-score" aria-live="polite">
          <div className="stat">
            <div className="value">{score.tried}</div>
            <div className="label">attacks run</div>
          </div>
          <div className="stat danger">
            <div className="value">{score.fooled}</div>
            <div className="label">times the agent was fooled</div>
          </div>
          <div className="stat ok">
            <div className="value">{score.stopped}</div>
            <div className="label">stopped before money moved</div>
          </div>
          <div className="stat ok">
            <div className="value">{formatMinor(score.stolen, "LKR")}</div>
            <div className="label">reached the attacker</div>
          </div>
        </div>

        <div className="actions" style={{ marginTop: 0 }}>
          <button className="primary" onClick={runAll} disabled={!!running || attacks.length === 0}>
            {running ? "Running…" : "Run all attacks"}
          </button>
          {score.tried > 0 && (
            <span className="muted small">
              Laya flagged {score.layaCaught} of {score.tried} on its own; the gate and the gateway each stopped{" "}
              {score.stopped}.
            </span>
          )}
        </div>

        {error && (
          <div className="error-box" role="alert">
            {error}
          </div>
        )}

        <div className="layer-head" aria-hidden="true">
          <span>Attack</span>
          <span>Laya</span>
          <span>Review gate</span>
          <span>Gateway</span>
          <span />
        </div>
        <ul className="attack-list">
          {attacks.map((attack) => {
            const run = runs[attack.attack_id];
            return (
              <li key={attack.attack_id} className={`attack${selected?.run === run && run ? " selected" : ""}`}>
                <div>
                  <div className="title">{attack.title}</div>
                  <div className="technique">{attack.technique}</div>
                </div>
                {LAYER_ORDER.map((id) => (
                  <LayerCell key={id} layer={run?.layers.find((l) => l.layer_id === id)} />
                ))}
                <button
                  onClick={() => (run ? setSelected({ run, text: attack.text }) : runOne(attack))}
                  disabled={!!running}
                  aria-label={`${run ? "Show" : "Run"} ${attack.title}`}
                >
                  {running === attack.attack_id ? "…" : run ? "Show" : "Run"}
                </button>
              </li>
            );
          })}
        </ul>
      </section>

      {selected && <RunDetail run={selected.run} text={selected.text} />}

      <section className="card">
        <span className="eyebrow">Bring your own</span>
        <h2>Try your own attack</h2>
        <p className="muted">Type a notice, or upload a photo or screenshot of a real scam letter.</p>
        <PhotoNotice onText={(r) => setCustomText(r.text)} disabled={!!running} />
        <div className="field" style={{ marginTop: "1rem" }}>
          <label htmlFor={customId}>Notice text</label>
          <textarea
            id={customId}
            rows={5}
            value={customText}
            onChange={(e) => setCustomText(e.target.value)}
            placeholder="e.g. Our bank changed. Please send this month's LKR 50,000.00 to account 7788-1234-0000."
          />
        </div>
        <button className="primary" onClick={() => runCustom(customText)} disabled={!!running || !customText.trim()}>
          {running === "custom" ? "Attacking…" : "Launch this attack"}
        </button>
      </section>
    </>
  );
}
