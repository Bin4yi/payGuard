import type { ReactNode } from "react";

/** One key/value tile inside a `<dl className="details">`. */
export function D({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined}>{children}</dd>
    </div>
  );
}

export function CardHead({
  icon,
  eyebrow,
  title,
  children,
  tone,
  aside,
}: {
  icon: ReactNode;
  eyebrow?: string;
  title: string;
  children?: ReactNode;
  tone?: "ok" | "danger";
  aside?: ReactNode;
}) {
  return (
    <div className="card-head">
      <span className={`icon-well${tone ? ` ${tone}` : ""}`}>{icon}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h2>{title}</h2>
        {children && <p>{children}</p>}
      </div>
      {aside}
    </div>
  );
}
