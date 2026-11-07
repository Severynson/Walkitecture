import { lazy, Suspense, useEffect, useState } from "react";
import FixtureLinks from "./viewer/FixtureLinks";

// Babylon.js is large: load the viewer only when a scene is opened.
const FixtureViewer = lazy(() => import("./viewer/FixtureViewer"));

// Placeholder shell. Track E replaces this with react-router routes (/login, /register, /plans,
// /plans/:id) and renders viewer/ViewerPage for a plan. See IMPLEMENTATION_PLAN.md.

export default function App() {
  if (window.location.pathname.startsWith("/viewer")) {
    return (
      <Suspense fallback={<p style={{ font: "15px system-ui, sans-serif", padding: 16 }}>Loading 3D viewer…</p>}>
        <FixtureViewer />
      </Suspense>
    );
  }
  return <Home />;
}

type Health = Record<string, unknown>;

function Home() {
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
      <h2>Walk a sample plan</h2>
      <FixtureLinks />
      <p>
        Login, uploads, and editing are under construction. The API is live: <a href="/docs">/docs</a>
      </p>
      <h2>API health</h2>
      {error && <p style={{ color: "crimson" }}>Unavailable: {error}</p>}
      {health && <pre style={{ background: "#f4f4f4", padding: 12, overflowX: "auto" }}>{JSON.stringify(health, null, 2)}</pre>}
      {!health && !error && <p>Checking…</p>}
    </main>
  );
}
