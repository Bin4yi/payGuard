import { Bot, Camera, Flask, Gate, Key, Radar, Receipt, ShieldCheck, Signature } from "../components/Icons";

const FEATURES = [
  { icon: Signature, title: "Signed approval", body: "You approve one exact payment. That snapshot is signed, and nothing a document says can change it." },
  { icon: Key, title: "A 5-minute grant", body: "The agent gets a credential for this one payment only. It expires fast and can't be widened." },
  { icon: Bot, title: "An agent that only proposes", body: "Gemini reads the notice and suggests a payment. It never holds a key or moves money." },
  { icon: Gate, title: "An exact-match gateway", body: "Every field of the request must equal the signed approval. Anything else is refused." },
  { icon: Radar, title: "Laya pre-screen", body: "A fast decision model flags payee changes before the agent runs. It can add review, never approval." },
  { icon: Receipt, title: "Signed receipt chain", body: "Every decision is signed and hash-chained, then exported as portable, verifiable evidence." },
  { icon: Camera, title: "Photo notices", body: "Snap a paper notice. It's read on your device, then checked like any other untrusted text." },
  { icon: Flask, title: "Attack Lab", body: "Throw real scam techniques at a fully compromised agent and watch each layer stop them." },
];

export function Welcome({ busy, onStart, onLab }: { busy: boolean; onStart: () => void; onLab: () => void }) {
  return (
    <>
      <section className="hero">
        <div className="hero-mark">
          <span style={{ color: "var(--accent)" }}>
            <ShieldCheck size={52} />
          </span>
        </div>
        <h1>
          The AI proposes. <span className="grad">Only you approve.</span>
        </h1>
        <p className="lede">
          PayGuard lets a Gemini agent pay your rent, and makes it impossible for a poisoned notice, a scam letter or a
          confused model to pay anyone else, or pay twice.
        </p>
        <div className="actions">
          <button className="primary big" onClick={onStart} disabled={busy}>
            {busy ? "Starting…" : "Try demo"}
          </button>
          <button className="big" onClick={onLab} disabled={busy}>
            Open the Attack Lab
          </button>
        </div>
        <p className="muted small">Synthetic and isolated: no account, no personal data, no real money.</p>
      </section>
      <section aria-label="What PayGuard does" className="feature-grid">
        {FEATURES.map(({ icon: Icon, title, body }) => (
          <article key={title} className="feature">
            <span className="icon-well">
              <Icon />
            </span>
            <h3>{title}</h3>
            <p>{body}</p>
          </article>
        ))}
      </section>
    </>
  );
}
