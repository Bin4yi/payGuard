import { useCallback, useEffect, useState } from "react";
import {
  ApiError,
  NetworkError,
  confirmMandate,
  createAnonymousSession,
  createJob,
  createMandate,
  createScenario,
  exportReceipts,
  getCurrentScenario,
  getJob,
  getJobReceipts,
  getMandate,
  renewApproval,
  reviewAccountChange,
  revokeMandate,
  runJob,
  updateRentInstruction,
  type ExportResult,
  type Job,
  type Mandate,
  type OrchestrationResult,
  type ReceiptTrail,
  type Scenario,
  type Session,
} from "./api";
import { Dashboard } from "./views/Dashboard";
import { Approval } from "./views/Approval";
import { AgentActivity } from "./views/AgentActivity";
import { ResultView } from "./views/ResultView";
import { ReceiptTrailView } from "./views/ReceiptTrailView";
import { AccountChangeReview } from "./views/AccountChangeReview";
import { AttackLab } from "./views/AttackLab";
import { Welcome } from "./views/Welcome";
import { ShieldCheck } from "./components/Icons";

type ViewName = "welcome" | "dashboard" | "approval" | "activity" | "result" | "receipts" | "review" | "lab";

/** An action whose outcome we could not observe (SPEC.md F09). */
interface PendingOutcome {
  jobId: string;
  authoritative: Job | null;
}

const SESSION_STORAGE_KEY = "payguard-demo-session";

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [scenario, setScenario] = useState<Scenario | null>(null);
  const [mandate, setMandate] = useState<Mandate | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [runResult, setRunResult] = useState<OrchestrationResult | null>(null);
  const [receipts, setReceipts] = useState<ReceiptTrail | null>(null);
  const [exported, setExported] = useState<ExportResult | null>(null);
  const [pending, setPending] = useState<PendingOutcome | null>(null);
  const [view, setView] = useState<ViewName>("welcome");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Re-read everything the UI shows from the server (the only source of truth). */
  const refresh = useCallback(async (s: Session) => {
    const sc = await getCurrentScenario(s);
    setScenario(sc);
    const ref = sc.current_mandate;
    if (!ref) {
      setMandate(null);
      setJob(null);
      return sc;
    }
    setMandate(await getMandate(s, ref.mandate_id));
    setJob(ref.job_id ? await getJob(s, ref.job_id) : null);
    return sc;
  }, []);

  // Restore a previous session — including its approval and job, so the
  // navigation still works after a page reload.
  useEffect(() => {
    let saved: Session | null = null;
    try {
      const raw = window.localStorage.getItem(SESSION_STORAGE_KEY);
      saved = raw ? (JSON.parse(raw) as Session) : null;
    } catch {
      saved = null;
    }
    if (!saved) return;
    const s = saved;
    setSession(s);
    refresh(s)
      .then(() => setView("dashboard"))
      .catch(() => setView("welcome")); // e.g. the local demo server restarted
  }, [refresh]);

  const runSafely = useCallback(async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
    } catch (err) {
      if (err instanceof ApiError) setError(err.message);
      else if (err instanceof NetworkError) setError(err.message);
      else setError(String(err));
    } finally {
      setBusy(false);
    }
  }, []);

  const resetLocalState = () => {
    setMandate(null);
    setJob(null);
    setRunResult(null);
    setReceipts(null);
    setExported(null);
    setPending(null);
  };

  const handleTryDemo = (then: ViewName = "dashboard") =>
    runSafely(async () => {
      const s = await createAnonymousSession();
      try {
        window.localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(s));
      } catch {
        /* private mode: the session simply won't survive a reload */
      }
      setSession(s);
      setScenario(await createScenario(s));
      resetLocalState();
      setView(then);
    });

  const handleResetScenario = () =>
    runSafely(async () => {
      if (!session) return;
      setScenario(await createScenario(session));
      resetLocalState();
      setNotice("Started a fresh scenario. Your previous one — paid or not — is untouched.");
      setView("dashboard");
    });

  const handleUpdateAmount = (amountMinor: number) =>
    runSafely(async () => {
      if (!session) return;
      const updated = await updateRentInstruction(session, amountMinor);
      await refresh(session);
      setNotice(`Rent instruction updated (version ${updated.version}). New approvals will use it.`);
    });

  const handleApprove = (documentIds: string[]) =>
    runSafely(async () => {
      if (!session) return;
      const draft = await createMandate(session, documentIds);
      const confirmed = await confirmMandate(session, draft.mandate_id, draft.version, draft.nonce!);
      setMandate(confirmed);
      if (confirmed.signing_state !== "SIGNED") {
        // Approval is recorded but not yet signed — no grant can activate.
        setNotice("Approval recorded; signing is still pending. The agent can't run until it completes.");
        setView("dashboard");
        return;
      }
      setJob(await createJob(session, confirmed.mandate_id));
      setRunResult(null);
      setView("activity");
    });

  /** Poll authoritative status after a response we never received. */
  const reconcile = useCallback(
    async (s: Session, jobId: string) => {
      for (let attempt = 0; attempt < 4; attempt++) {
        try {
          const authoritative = await getJob(s, jobId);
          setPending({ jobId, authoritative });
          if (authoritative.status !== "RUNNING" && authoritative.status !== "READY") {
            setJob(authoritative);
            await refresh(s);
            return;
          }
        } catch {
          /* still unreachable — keep the outcome marked unknown */
        }
        await new Promise((r) => setTimeout(r, 1500));
      }
    },
    [refresh],
  );

  const handleRunAgent = () =>
    runSafely(async () => {
      if (!session || !job) return;
      try {
        const result = await runJob(session, job.job_id);
        setRunResult(result);
        setPending(null);
        await refresh(session);
        setView("result");
      } catch (err) {
        if (err instanceof NetworkError) {
          // Never report an unobserved outcome as a failure (SPEC.md F09).
          setPending({ jobId: job.job_id, authoritative: null });
          setView("result");
          await reconcile(session, job.job_id);
          return;
        }
        throw err;
      }
    });

  const handleReplay = () =>
    runSafely(async () => {
      if (!session || !job) return;
      setRunResult(await runJob(session, job.job_id));
      await refresh(session);
    });

  const handleViewReceipts = () =>
    runSafely(async () => {
      if (!session || !job) return;
      setReceipts(await getJobReceipts(session, job.job_id));
      setExported(null);
      setView("receipts");
    });

  const handleExport = () =>
    runSafely(async () => {
      if (!session || !job) return;
      setExported(await exportReceipts(session, job.job_id));
    });

  const handleRevoke = () =>
    runSafely(async () => {
      if (!session || !mandate) return;
      await revokeMandate(session, mandate.mandate_id);
      await refresh(session);
      setRunResult(null);
      setNotice("Approval revoked. Its grant can no longer be used; approve again whenever you're ready.");
      setView("dashboard");
    });

  const confirmReplacement = async (s: Session, draft: Mandate) => {
    const confirmed = await confirmMandate(s, draft.mandate_id, draft.version, draft.nonce!);
    setMandate(confirmed);
    setJob(await createJob(s, confirmed.mandate_id));
    setRunResult(null);
    await refresh(s);
    setView("activity");
  };

  const handleAccountChange = (newAccountNumber: string, newLabel: string) =>
    runSafely(async () => {
      if (!session || !mandate) return;
      await confirmReplacement(session, await reviewAccountChange(session, mandate.mandate_id, newAccountNumber, newLabel));
      setNotice("Account change approved. The previous approval is superseded and can no longer pay.");
    });

  const handleRenew = () =>
    runSafely(async () => {
      if (!session || !mandate) return;
      await confirmReplacement(session, await renewApproval(session, mandate.mandate_id));
      setNotice("Approval renewed with the same payment details.");
    });

  const obligationPaid = scenario?.obligation_state === "PAID";
  const canReview = !!mandate && mandate.state === "APPROVED" && mandate.signing_state === "SIGNED" && !obligationPaid;

  return (
    <>
      <header className="app-header">
        <span className="brand">
          <span className="brand-mark" style={{ color: "var(--accent)" }}>
            <ShieldCheck size={20} />
          </span>
          PayGuard
          <span className="sim-chip" role="status" title="Simulation: synthetic data, no real money moves">
            Simulation
          </span>
        </span>
        {view !== "welcome" && (
          <nav className="nav" aria-label="Demo views">
            <button aria-current={view === "dashboard" ? "page" : undefined} onClick={() => setView("dashboard")}>
              Dashboard
            </button>
            <button aria-current={view === "approval" ? "page" : undefined} onClick={() => setView("approval")} disabled={!scenario}>
              Approval
            </button>
            <button aria-current={view === "activity" ? "page" : undefined} onClick={() => setView("activity")} disabled={!job}>
              Agent activity
            </button>
            <button
              aria-current={view === "result" ? "page" : undefined}
              onClick={() => setView("result")}
              disabled={!runResult && !pending}
            >
              Result
            </button>
            <button aria-current={view === "receipts" ? "page" : undefined} onClick={handleViewReceipts} disabled={!job || busy}>
              Receipt trail
            </button>
            <button aria-current={view === "review" ? "page" : undefined} onClick={() => setView("review")} disabled={!canReview}>
              Account change
            </button>
            <button className="lab-tab" aria-current={view === "lab" ? "page" : undefined} onClick={() => setView("lab")} disabled={!session}>
              Attack Lab
            </button>
          </nav>
        )}
      </header>

      {error && (
        <div className="error-box" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice-box" role="status">
          {notice}
        </div>
      )}

      {view === "welcome" && (
        <Welcome busy={busy} onStart={() => handleTryDemo("dashboard")} onLab={() => handleTryDemo("lab")} />
      )}

      {view === "lab" && session && <AttackLab session={session} />}

      {view === "dashboard" && scenario && (
        <Dashboard
          scenario={scenario}
          mandate={mandate}
          job={job}
          busy={busy}
          onReset={handleResetScenario}
          onRevoke={handleRevoke}
          onUpdateAmount={handleUpdateAmount}
          onGoApprove={() => setView("approval")}
          onGoActivity={() => setView("activity")}
        />
      )}

      {view === "approval" && scenario && session && (
        <Approval
          scenario={scenario}
          mandate={mandate}
          session={session}
          busy={busy}
          onApprove={handleApprove}
          onReset={handleResetScenario}
          onGoActivity={() => setView("activity")}
        />
      )}

      {view === "activity" && job && (
        <AgentActivity job={job} mandate={mandate} busy={busy} onRun={handleRunAgent} onRenew={handleRenew} />
      )}

      {view === "result" && (runResult || pending) && (
        <ResultView
          result={runResult}
          pending={pending}
          busy={busy}
          onReplay={handleReplay}
          onViewReceipts={handleViewReceipts}
          onCheckAgain={() => session && pending && runSafely(() => reconcile(session, pending.jobId))}
          onGoReview={() => setView("review")}
          canReview={canReview}
        />
      )}

      {view === "receipts" && receipts && (
        <ReceiptTrailView trail={receipts} exported={exported} busy={busy} onExport={handleExport} />
      )}

      {view === "review" && mandate && (
        <AccountChangeReview
          mandate={mandate}
          busy={busy}
          onSubmit={handleAccountChange}
          onRenew={handleRenew}
          onRevoke={handleRevoke}
        />
      )}
    </>
  );
}
