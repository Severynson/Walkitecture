import { useEffect, useState } from "react";
import { FIXTURE_NAMES, loadFixture } from "./fixtures";
import type { Viewer } from "./engine";
import type { SceneResponse } from "./types";
import ViewerPage from "./ViewerPage";

/** `/viewer?fixture=<name>[&view=top]`: walk a committed fixture without logging in or a running backend. */
export default function FixtureViewer() {
  const params = new URLSearchParams(window.location.search);
  const name = params.get("fixture") ?? FIXTURE_NAMES[0] ?? "";
  const initialMode = params.get("view") === "top" ? "top-down" : undefined;
  const [scene, setScene] = useState<SceneResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [viewer, setViewer] = useState<Viewer | null>(null);

  useEffect(() => {
    loadFixture(name).then(setScene, (e: Error) => setError(e.message));
  }, [name]);

  if (error) {
    return (
      <main className="page">
        <p className="error">{error}</p>
        <ul>
          {FIXTURE_NAMES.map((n) => (
            <li key={n}>
              <a href={`/viewer?fixture=${encodeURIComponent(n)}`}>{n}</a>
            </li>
          ))}
        </ul>
      </main>
    );
  }
  return (
    <ViewerPage
      scene={scene}
      loadKey={name}
      title={`Sample: ${name}`}
      initialMode={initialMode}
      onReady={setViewer}
      onSelect={(meta) => viewer?.select(meta?.objectId ?? null)}
      hudExtra={
        <div className="hud-links">
          <a href="/login">Log in</a> to upload your own DWG and edit it.
        </div>
      }
    />
  );
}
