import { Bot, Check, Cross, Gate, Key, Receipt, Signature } from "./Icons";

export type StepState = "todo" | "done" | "stop";

const STEPS = [
  { id: "approve", label: "You approve", icon: Signature },
  { id: "grant", label: "Grant issued", icon: Key },
  { id: "agent", label: "Agent proposes", icon: Bot },
  { id: "gateway", label: "Gateway checks", icon: Gate },
  { id: "receipt", label: "Signed receipt", icon: Receipt },
] as const;

export type StepId = (typeof STEPS)[number]["id"];

/** The five-step trust path, lit up as far as this payment has got. */
export function Pipeline({ states }: { states: Partial<Record<StepId, StepState>> }) {
  return (
    <ol className="pipeline" aria-label="Payment progress">
      {STEPS.map(({ id, label, icon: Icon }) => {
        const state = states[id] ?? "todo";
        const word = state === "done" ? "done" : state === "stop" ? "stopped" : "not yet";
        return (
          <li key={id} className={state === "todo" ? undefined : state}>
            <span className="dot">
              {state === "done" ? <Check size={18} /> : state === "stop" ? <Cross size={18} /> : <Icon size={18} />}
            </span>
            {label}
            <span className="sr-only"> ({word})</span>
          </li>
        );
      })}
    </ol>
  );
}
