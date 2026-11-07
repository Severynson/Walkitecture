import { useEffect, useRef, useState } from "react";
import type { ViewMode } from "./cameras";
import { Viewer } from "./engine";
import type { SceneResponse } from "./types";

interface Props {
  /** Scene to show (from GET /api/plans/{id}/scene or a fixture); null while loading. */
  scene: SceneResponse | null;
  title?: string;
  /** View to start in (e.g. from `?view=top`). */
  initialMode?: ViewMode;
  /** Called once with the viewer so parents (edit panel, picking) can drive it. */
  onReady?: (viewer: Viewer) => void;
}

/** Full-screen Babylon canvas + HUD. Overlays from track E (edit panel, chat) go next to the HUD. */
export default function ViewerPage({ scene, title, initialMode, onReady }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewerRef = useRef<Viewer | null>(null);
  const [mode, setMode] = useState<ViewMode>("first-person");
  const [locked, setLocked] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const viewer = new Viewer(canvas);
    viewerRef.current = viewer;
    onReady?.(viewer);

    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.code === "KeyT") setMode(viewer.toggleMode());
    };
    const onLockChange = () => setLocked(document.pointerLockElement === canvas);
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerlockchange", onLockChange);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerlockchange", onLockChange);
      viewer.dispose();
      viewerRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the viewer lives as long as the page
  }, []);

  useEffect(() => {
    if (scene && viewerRef.current) {
      viewerRef.current.load(scene.babylon);
      if (initialMode) viewerRef.current.setMode(initialMode);
      setMode(viewerRef.current.cameras.mode);
    }
  }, [scene, initialMode]);

  const lookAround = () => {
    if (mode === "first-person" && document.pointerLockElement !== canvasRef.current) {
      void canvasRef.current?.requestPointerLock();
    }
  };

  const stats = scene
    ? `${scene.model.walls?.length ?? 0} walls · ${scene.model.rooms?.length ?? 0} rooms · ${scene.model.furniture?.length ?? 0} furniture`
    : "Loading…";

  return (
    <div style={{ position: "fixed", inset: 0, background: "#dde5ee" }}>
      <canvas
        ref={canvasRef}
        onClick={lookAround}
        style={{ width: "100%", height: "100%", display: "block", outline: "none", touchAction: "none" }}
      />
      <div style={hudStyle}>
        <strong>{title ?? "Walkitecture"}</strong>
        <div>{stats}</div>
        <div style={{ marginTop: 6, opacity: 0.85 }}>
          {mode === "first-person"
            ? locked
              ? "WASD / arrows to walk · mouse to look · Esc to release · T top-down"
              : "Click the view to look around · WASD / arrows to walk · T top-down"
            : "Drag to orbit · right-drag to pan · wheel to zoom · T first person"}
        </div>
        <button type="button" onClick={() => viewerRef.current && setMode(viewerRef.current.toggleMode())} style={buttonStyle}>
          {mode === "first-person" ? "Top-down view (T)" : "First person (T)"}
        </button>
      </div>
    </div>
  );
}

const hudStyle: React.CSSProperties = {
  position: "absolute",
  top: 12,
  left: 12,
  maxWidth: "calc(100% - 24px)",
  padding: "10px 12px",
  borderRadius: 8,
  background: "rgba(20, 24, 31, 0.72)",
  color: "#f4f6f8",
  font: "13px/1.4 system-ui, sans-serif",
  pointerEvents: "auto",
};

const buttonStyle: React.CSSProperties = {
  marginTop: 8,
  padding: "4px 10px",
  borderRadius: 6,
  border: "1px solid rgba(255,255,255,0.35)",
  background: "rgba(255,255,255,0.12)",
  color: "inherit",
  font: "inherit",
  cursor: "pointer",
};
