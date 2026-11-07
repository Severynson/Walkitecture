import { useEffect, useState } from "react";
import FixtureLinks from "./FixtureLinks";
import { FIXTURE_NAMES, loadFixture } from "./fixtures";
import type { SceneResponse } from "./types";
import ViewerPage from "./ViewerPage";

/** `/viewer?fixture=<name>`: walk a committed fixture without logging in or a running backend. */
export default function FixtureViewer() {
  const params = new URLSearchParams(window.location.search);
  const name = params.get("fixture") ?? FIXTURE_NAMES[0] ?? "";
  const initialMode = params.get("view") === "top" ? "top-down" : undefined;
  const [scene, setScene] = useState<SceneResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadFixture(name).then(setScene, (e: Error) => setError(e.message));
  }, [name]);

  if (error) {
    return (
      <main style={{ font: "15px system-ui, sans-serif", margin: "3rem auto", maxWidth: 640, padding: "0 16px" }}>
        <p style={{ color: "crimson" }}>{error}</p>
        <FixtureLinks />
      </main>
    );
  }
  return <ViewerPage scene={scene} title={`Fixture: ${name}`} initialMode={initialMode} />;
}
