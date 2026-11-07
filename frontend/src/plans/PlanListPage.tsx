import { type ChangeEvent, type DragEvent, useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { errorMessage } from "../api/client";
import * as endpoints from "../api/endpoints";
import type { PlanSummary } from "../api/schema";
import { useAuth } from "../auth/AuthContext";

const MAX_MB = 25;

export default function PlanListPage() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const [plans, setPlans] = useState<PlanSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => {
    endpoints.listPlans().then(setPlans, (e) => setError(errorMessage(e)));
  }, []);
  useEffect(refresh, [refresh]);

  const upload = async (file: File) => {
    setError(null);
    if (!/\.(dwg|dxf)$/i.test(file.name)) {
      setError("Please choose a .dwg file (DXF works too).");
      return;
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      setError(`That file is larger than ${MAX_MB} MB.`);
      return;
    }
    setBusy(`Uploading and reading ${file.name}… this can take up to a minute for large drawings.`);
    try {
      const plan = await endpoints.uploadPlan(file);
      if (plan.status === "parsed") navigate(`/plans/${plan.id}`);
      else {
        setError(`${file.name}: ${plan.error ?? "the drawing could not be turned into a building."}`);
        refresh();
      }
    } catch (e) {
      setError(errorMessage(e));
      refresh();
    } finally {
      setBusy(null);
    }
  };

  const onPick = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) void upload(file);
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files?.[0];
    if (file) void upload(file);
  };

  const addSample = async () => {
    setError(null);
    setBusy("Creating the sample apartment…");
    try {
      const result = await endpoints.createDemoPlan();
      navigate(`/plans/${result.plan.id}`);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(null);
    }
  };

  const remove = async (plan: PlanSummary) => {
    if (!window.confirm(`Delete "${plan.filename}"? This cannot be undone.`)) return;
    try {
      await endpoints.deletePlan(plan.id);
      refresh();
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  return (
    <main className="page">
      <header className="topbar">
        <span className="brand">Walkitecture</span>
        <span className="spacer" />
        <span className="muted">{user?.username}</span>
        <button onClick={logout}>Log out</button>
      </header>

      <section
        className={`card dropzone${dragging ? " dragging" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <h2>Upload a floor plan</h2>
        <p className="muted">
          Drop a <strong>.dwg</strong> file here or choose one. Walls, doors, windows, rooms, and furniture are read from
          the drawing and turned into a walkable 3D space.
        </p>
        <div className="row">
          <button className="primary" disabled={!!busy} onClick={() => fileInput.current?.click()}>
            Choose DWG file
          </button>
          <button disabled={!!busy} onClick={addSample}>
            Add sample apartment
          </button>
          <input ref={fileInput} type="file" accept=".dwg,.dxf" hidden onChange={onPick} />
        </div>
        {busy && <p className="busy">{busy}</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <section className="card">
        <h2>Your plans</h2>
        {plans === null && !error && <p className="muted">Loading…</p>}
        {plans?.length === 0 && <p className="muted">No plans yet. Upload a DWG or add the sample apartment.</p>}
        {plans && plans.length > 0 && (
          <ul className="plan-list">
            {plans.map((plan) => (
              <li key={plan.id}>
                <div className="plan-main">
                  {plan.hasModel ? (
                    <Link to={`/plans/${plan.id}`} className="plan-name">
                      {plan.filename}
                    </Link>
                  ) : (
                    <span className="plan-name">{plan.filename}</span>
                  )}
                  <span className="muted small">
                    {plan.sourceFormat.toUpperCase()} · {formatSize(plan.fileSize)} · {new Date(plan.createdAt).toLocaleString()}
                  </span>
                  {plan.status === "failed" && <span className="error small">{plan.error ?? "Could not be read."}</span>}
                </div>
                <span className={`badge ${plan.status}`}>{plan.status}</span>
                {plan.hasModel && (
                  <Link className="button primary" to={`/plans/${plan.id}`}>
                    Walk
                  </Link>
                )}
                <button className="danger" onClick={() => remove(plan)} aria-label={`Delete ${plan.filename}`}>
                  Delete
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
