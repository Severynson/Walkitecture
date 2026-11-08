import { type ReactNode, useEffect, useRef, useState } from "react";
import type { ViewMode } from "./cameras";
import { type DoorFocus, Viewer } from "./engine";
import type { MeshMeta, SceneResponse } from "./types";

interface Props {
  /** Scene to show (GET /api/plans/{id}/scene or a fixture); null while loading. Drives the HUD stats. */
  scene: SceneResponse | null;
  /**
   * The scene is (re)loaded into the viewer when this key changes (and once `scene` is available).
   * Later changes (edits, undo, AI fixes) are applied by the parent through the Viewer from `onReady`.
   */
  loadKey: string;
  title?: string;
  /** View to start in (e.g. from `?view=top`). */
  initialMode?: ViewMode;
  /** Called once with the viewer so parents (edit panel, chat) can drive it. */
  onReady?: (viewer: Viewer) => void;
  /** Click on an object (crosshair in first person, pointer in top-down); null = clicked nothing. */
  onSelect?: (meta: MeshMeta | null) => void;
  /** Overlays shown on the right (edit panel, chat). */
  children?: ReactNode;
  /** Extra HUD lines/links under the stats. */
  hudExtra?: ReactNode;
}

const DRAG_TOLERANCE_PX = 5;

/** Full-screen Babylon canvas + HUD + crosshair; overlays from the parent go on the right. */
export default function ViewerPage({ scene, loadKey, title, initialMode, onReady, onSelect, children, hudExtra }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const viewerRef = useRef<Viewer | null>(null);
  const loadedKey = useRef<string | null>(null);
  const downAt = useRef<{ x: number; y: number } | null>(null);
  const [mode, setMode] = useState<ViewMode>("first-person");
  const [locked, setLocked] = useState(false);
  const [door, setDoor] = useState<DoorFocus | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current!;
    const viewer = new Viewer(canvas);
    viewerRef.current = viewer;
    viewer.onDoorFocus = setDoor;
    onReady?.(viewer);

    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)) return;
      if (event.code === "KeyT") setMode(viewer.toggleMode());
      if (event.code === "KeyE" && !event.repeat) viewer.toggleDoor();
    };
    const onLockChange = () => setLocked(document.pointerLockElement === canvas);
    window.addEventListener("keydown", onKey);
    document.addEventListener("pointerlockchange", onLockChange);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerlockchange", onLockChange);
      viewer.dispose();
      viewerRef.current = null;
      loadedKey.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the viewer lives as long as the page
  }, []);

  useEffect(() => {
    const viewer = viewerRef.current;
    if (!scene || !viewer || loadedKey.current === loadKey) return;
    viewer.load(scene.babylon);
    if (initialMode && loadedKey.current === null) viewer.setMode(initialMode);
    loadedKey.current = loadKey;
    setMode(viewer.cameras.mode);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reload only on a new key (see Props.loadKey)
  }, [loadKey, scene === null]);

  const onPointerDown = (event: React.PointerEvent) => {
    downAt.current = { x: event.clientX, y: event.clientY };
  };

  const onClick = (event: React.MouseEvent<HTMLCanvasElement>) => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const start = downAt.current;
    if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > DRAG_TOLERANCE_PX) return; // orbit drag
    if (mode === "first-person") {
      if (document.pointerLockElement !== canvasRef.current) {
        void canvasRef.current?.requestPointerLock();
        return;
      }
      onSelect?.(viewer.pick());
    } else {
      const rect = event.currentTarget.getBoundingClientRect();
      onSelect?.(viewer.pick(event.clientX - rect.left, event.clientY - rect.top));
    }
  };

  const toggle = () => viewerRef.current && setMode(viewerRef.current.toggleMode());

  const stats = scene
    ? `${scene.model.walls?.length ?? 0} walls · ${scene.model.rooms?.length ?? 0} rooms · ${scene.model.furniture?.length ?? 0} furniture`
    : "Loading…";

  return (
    <div className="viewer">
      <canvas ref={canvasRef} onPointerDown={onPointerDown} onClick={onClick} className="viewer-canvas" />
      {mode === "first-person" && locked && <div className="crosshair" aria-hidden />}
      {mode === "first-person" && door && (
        <div className="door-hint" role="status">
          Press <kbd>E</kbd> to {door.open ? "close" : "open"} the door
        </div>
      )}
      <div className="hud">
        <strong>{title ?? "Walkitecture"}</strong>
        <div>{stats}</div>
        <div className="hud-help">
          {mode === "first-person"
            ? locked
              ? "WASD / arrows walk · Shift runs · E opens doors · mouse looks · click selects · Esc frees the mouse · T top-down"
              : "Click the view to look around · WASD / arrows to walk · Shift to run · E opens doors · T top-down"
            : "Click selects · drag orbits · right-drag pans · wheel zooms · T first person"}
        </div>
        <div className="row">
          <button type="button" onClick={toggle}>
            {mode === "first-person" ? "Top-down view (T)" : "First person (T)"}
          </button>
        </div>
        {hudExtra}
      </div>
      {children && <div className="overlays">{children}</div>}
    </div>
  );
}
