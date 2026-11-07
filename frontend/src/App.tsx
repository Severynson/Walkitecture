import { useEffect, useState } from "react";

// Placeholder shell (Phase 0). Track E replaces this with routing + auth pages,
// track B adds the viewer under src/viewer/. See IMPLEMENTATION_PLAN.md.

type Health = { status?: string; database?: string; dwgConverter?: boolean } & Record<string, unknown>;

export default function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setHealth)
      .catch((e: Error) => setError(e.message));
  }, []);

  return (
    <main style={{ fontFamily: "system-ui, sans-serif", maxWidth: 640, margin: "4rem auto", padding: "0 16px" }}>
      <h1>Walkitecture</h1>
      <p>Don't just see the floor plan. Walk it.</p>
      <p>
        The walkthrough UI is under construction. The API is live: <a href="/docs">/docs</a>
      </p>
      <h2>API health</h2>
      {error && <p style={{ color: "crimson" }}>Unavailable: {error}</p>}
      {health && <pre style={{ background: "#f4f4f4", padding: 12, overflowX: "auto" }}>{JSON.stringify(health, null, 2)}</pre>}
      {!health && !error && <p>Checking…</p>}
    </main>
  );
}
