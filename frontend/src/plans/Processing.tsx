import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { PlanDetail } from "../api/schema";
import { Logo } from "../brand/Logo";

/** Pipeline steps, in order (backend app/services/pipeline.py STAGES). AI steps may be skipped. */
const STAGES: [string, string][] = [
  ["queued", "Waiting to start"],
  ["converting", "Converting the DWG"],
  ["reading", "Reading walls, doors, windows, rooms and furniture"],
  ["classifying", "AI: classifying the drawing's layers and blocks"],
  ["rebuilding", "Rebuilding with the AI's layer roles"],
  ["furniture", "AI: identifying unknown furniture"],
  ["checking", "Checking furniture, windows and room entrances"],
  ["reviewing", "AI: deciding how to fix what the checks found"],
  ["fixing", "Applying the fixes"],
  ["saving", "Building the 3D model"],
];

export function stageLabel(stage: string | null | undefined): string {
  return STAGES.find(([id]) => id === stage)?.[1] ?? "Processing";
}

/** Shown while a plan is processed in the background (status "processing") or after it failed. */
export default function Processing({ plan, onRetry, retrying }: { plan: PlanDetail; onRetry(): void; retrying: boolean }) {
  const [seconds, setSeconds] = useState(0);
  const processing = plan.status === "processing";
  useEffect(() => {
    if (!processing) return;
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.round((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [processing]);

  const current = STAGES.findIndex(([id]) => id === plan.stage);
  return (
    <main className="page">
      <header className="topbar">
        <Logo />
        <span className="spacer" />
        <Link to="/plans">← Your plans</Link>
      </header>
      <section className="card processing" aria-live="polite">
        <span className="sheet-tag">A-201 · {plan.filename}</span>
        {processing ? (
          <>
            <h2>Preparing your walkthrough…</h2>
            <p className="muted">
              Every drawing is read, classified and checked by AI before its 3D model is built, so nothing impossible ends
              up in it. This usually takes one to four minutes; you can leave this page and come back.
            </p>
            <ol className="stages">
              {STAGES.slice(1).map(([id, label], index) => {
                const state = index + 1 < current ? "done" : index + 1 === current ? "active" : "todo";
                return (
                  <li key={id} className={state}>
                    {label}
                  </li>
                );
              })}
            </ol>
            <p className="muted small">{seconds > 0 ? `${seconds} s` : "Starting…"}</p>
          </>
        ) : (
          <>
            <h2>This drawing could not be turned into a building</h2>
            <p className="error" role="alert">
              {plan.error ?? "Unknown error."}
            </p>
            <div className="row">
              <button className="primary" disabled={retrying} onClick={onRetry}>
                {retrying ? "Starting…" : "Retry"}
              </button>
              <Link to="/plans">Back to your plans</Link>
            </div>
          </>
        )}
      </section>
    </main>
  );
}
