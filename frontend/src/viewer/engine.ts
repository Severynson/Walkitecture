import {
  type AbstractMesh,
  Color3,
  Color4,
  DirectionalLight,
  Engine,
  HemisphericLight,
  HighlightLayer,
  InstancedMesh,
  Mesh,
  Ray,
  Scene,
  StandardMaterial,
  Vector3,
} from "@babylonjs/core";
import { Cameras, type ViewMode } from "./cameras";
import { boxObjectId, createBox } from "./load/boxes";
import { FurnitureLibrary } from "./load/furniture";
import { createRoomLabel } from "./load/labels";
import { Materials } from "./load/materials";
import { Registry } from "./registry";
import type { BabylonSceneSpec, MeshMeta } from "./types";

const GRAVITY = -9.81 / 60; // per frame at 60 fps
const SPAWN_RAYS = 72; // every 5°
const SPAWN_FOV = (80 * Math.PI) / 180;
const MAX_SIGHTLINE = 100; // m
const SELECTION_COLOR = Color3.FromHexString("#ff9f1c");
/** Pick targets that are "the building" rather than an editable object. */
const NOT_SELECTABLE = new Set(["floor", "ceiling"]);

function e2eEnabled(): boolean {
  try {
    return localStorage.getItem("walkitecture.e2e") === "1";
  } catch {
    return false;
  }
}

/**
 * React-agnostic viewer: owns the Babylon engine/scene and turns the backend's scene spec into meshes.
 * `load` builds everything; `applyUpdate` rebuilds only the objects listed in an edit's `affectedIds`.
 */
export class Viewer {
  readonly engine: Engine;
  readonly scene: Scene;
  readonly cameras: Cameras;
  private readonly registry = new Registry();
  private readonly materials: Materials;
  private readonly furniture: FurnitureLibrary;
  private readonly highlight: HighlightLayer;
  private readonly selectionMaterial: StandardMaterial;
  private spec: BabylonSceneSpec | null = null;
  private selectedId: string | null = null;
  private readonly onResize = () => this.engine.resize();

  constructor(canvas: HTMLCanvasElement) {
    this.engine = new Engine(canvas, true, { preserveDrawingBuffer: false, stencil: true });
    const scene = new Scene(this.engine);
    scene.clearColor = new Color4(0.86, 0.9, 0.95, 1);
    scene.ambientColor = new Color3(0.3, 0.3, 0.3);
    scene.collisionsEnabled = true;
    scene.gravity = new Vector3(0, GRAVITY, 0);
    this.scene = scene;

    const sky = new HemisphericLight("sky", new Vector3(0.2, 1, 0.1), scene);
    sky.intensity = 0.85;
    sky.groundColor = new Color3(0.45, 0.42, 0.4);
    const sun = new DirectionalLight("sun", new Vector3(-0.4, -1, -0.3), scene);
    sun.intensity = 0.35;

    this.highlight = new HighlightLayer("selection", scene, { isStroke: true });
    this.highlight.innerGlow = false;
    this.selectionMaterial = new StandardMaterial("selection-box", scene);
    this.selectionMaterial.diffuseColor = SELECTION_COLOR;
    this.selectionMaterial.emissiveColor = SELECTION_COLOR.scale(0.4);
    this.selectionMaterial.alpha = 0.25;

    this.cameras = new Cameras(scene, canvas);
    this.materials = new Materials(scene);
    this.furniture = new FurnitureLibrary(scene);

    this.engine.runRenderLoop(() => scene.render());
    window.addEventListener("resize", this.onResize);
    // Browser smoke test hook (frontend/e2e/smoke.mjs): dev builds, or localStorage "walkitecture.e2e" = "1".
    if (import.meta.env.DEV || e2eEnabled()) (window as unknown as { __viewer?: Viewer }).__viewer = this;
  }

  /**
   * Replaces whatever is shown with `spec`. A fresh load moves the walker to the spawn point;
   * `keepCamera` keeps both cameras where they are (after edits, AI fixes, undo).
   */
  load(spec: BabylonSceneSpec, { keepCamera = false } = {}): void {
    const walker = this.cameras.walker;
    const saved = keepCamera && this.spec ? { position: walker.position.clone(), rotation: walker.rotation.clone() } : null;
    this.registry.disposeAll();
    this.spec = spec;
    for (const box of spec.meshes) createBox(this.scene, box, spec, this.materials, this.registry);
    for (const room of spec.rooms) createRoomLabel(this.scene, room, this.registry);
    for (const item of spec.furniture) void this.furniture.place(item, this.registry);
    if (saved) {
      this.cameras.setBodySize(spec);
      walker.position.copyFrom(saved.position);
      walker.rotation.copyFrom(saved.rotation);
    } else {
      this.cameras.fit(spec);
      this.faceOpenSpace();
    }
    this.syncCeiling();
    this.select(this.selectedId);
  }

  /**
   * Applies a new spec after an edit without moving the camera. `affectedIds` are wall / opening /
   * furniture ids; "building" (global changes such as the ceiling height) rebuilds everything.
   */
  applyUpdate(spec: BabylonSceneSpec, affectedIds: string[]): void {
    if (!this.spec || affectedIds.includes("building")) {
      this.load(spec, { keepCamera: true });
      return;
    }
    const affected = new Set(affectedIds);
    // Openings belong to walls: rebuild the whole wall an affected opening sits in.
    for (const opening of [...spec.openings, ...this.spec.openings]) {
      if (affected.has(opening.id)) affected.add(opening.wallId);
    }
    for (const id of affected) this.registry.dispose(id);
    this.spec = spec;
    for (const box of spec.meshes) {
      if (affected.has(boxObjectId(box))) createBox(this.scene, box, spec, this.materials, this.registry);
    }
    for (const item of spec.furniture) if (affected.has(item.id)) void this.furniture.place(item, this.registry);
    this.syncCeiling();
    this.select(this.selectedId);
  }

  /**
   * What is under the crosshair (first person, pointer locked: screen center) or under the pointer
   * (x, y in canvas CSS pixels). Floor and ceiling count as "nothing".
   */
  pick(x?: number, y?: number): MeshMeta | null {
    const canvas = this.engine.getRenderingCanvas()!;
    const px = x ?? canvas.clientWidth / 2;
    const py = y ?? canvas.clientHeight / 2;
    const hit = this.scene.pick(px, py, (mesh) => mesh.isPickable && mesh.isVisible && !!mesh.metadata);
    const meta = hit?.pickedMesh?.metadata as MeshMeta | undefined;
    if (!meta || NOT_SELECTABLE.has(meta.objectId) || meta.kind === "ceiling" || meta.kind === "floor") return null;
    return meta;
  }

  /** Highlights every mesh of one object (wall, opening, furniture); null clears the selection. */
  select(objectId: string | null): void {
    this.selectedId = objectId;
    this.highlight.removeAllMeshes();
    for (const mesh of this.scene.meshes) {
      if (mesh.name.endsWith(":collider")) {
        mesh.isVisible = false;
        mesh.material = null;
      }
    }
    if (!objectId) return;
    for (const mesh of this.scene.meshes) {
      const meta = mesh.metadata as MeshMeta | undefined;
      if (meta?.objectId !== objectId) continue;
      if (mesh.name.endsWith(":collider")) {
        // Furniture: GLB parts are instances (no highlight support), so show the footprint box instead.
        mesh.material = this.selectionMaterial;
        mesh.isVisible = true;
        this.highlight.addMesh(mesh as Mesh, SELECTION_COLOR);
      } else if (mesh instanceof Mesh && !(mesh instanceof InstancedMesh)) {
        this.highlight.addMesh(mesh, SELECTION_COLOR);
      }
    }
  }

  /** Horizontal viewing direction in plan coordinates (plan y = Babylon z), for "move it back". */
  cameraForward(): [number, number] {
    const camera = this.scene.activeCamera!;
    const dir = camera.getDirection(Vector3.Forward());
    if (this.cameras.mode === "top-down" || Math.hypot(dir.x, dir.z) < 1e-3) {
      // Looking straight down: "forward" is screen-up, i.e. away from the overhead camera's position.
      const t = this.cameras.overhead.target.subtract(this.cameras.overhead.position);
      const len = Math.hypot(t.x, t.z);
      return len < 1e-6 ? [0, 1] : [t.x / len, t.z / len];
    }
    const len = Math.hypot(dir.x, dir.z);
    return [dir.x / len, dir.z / len];
  }

  setMode(mode: ViewMode): void {
    this.cameras.setMode(mode);
    this.syncCeiling();
  }

  toggleMode(): ViewMode {
    const mode = this.cameras.toggle();
    this.syncCeiling();
    return mode;
  }

  /**
   * Turns the walker toward the longest sightline that stays inside the building, so you don't spawn
   * staring at a wall — or out of an open front door at the sky (rays that hit nothing don't count).
   */
  private faceOpenSpace(): void {
    const origin = this.cameras.walker.position.clone();
    const solid = (mesh: AbstractMesh) => mesh.checkCollisions && mesh.isEnabled();
    // Sightline length in every direction (0 when the ray leaves the building).
    const distances = Array.from({ length: SPAWN_RAYS }, (_, i) => {
      const yaw = (i / SPAWN_RAYS) * Math.PI * 2;
      const hit = this.scene.pickWithRay(new Ray(origin, new Vector3(Math.sin(yaw), 0, Math.cos(yaw)), MAX_SIGHTLINE), solid);
      return hit?.hit ? hit.distance : 0;
    });
    // Score each heading by the mean sightline across the field of view, so the view runs along the
    // room (not into its farthest corner, which is the longest single ray in a rectangular room).
    const half = Math.round((SPAWN_FOV / 2 / (Math.PI * 2)) * SPAWN_RAYS);
    let best = { score: -1, yaw: this.cameras.walker.rotation.y };
    for (let i = 0; i < SPAWN_RAYS; i++) {
      let sum = 0;
      for (let k = -half; k <= half; k++) sum += distances[(i + k + SPAWN_RAYS) % SPAWN_RAYS];
      const score = sum / (2 * half + 1);
      if (score > best.score + 1e-3) best = { score, yaw: (i / SPAWN_RAYS) * Math.PI * 2 };
    }
    this.cameras.walker.rotation.set(0, best.yaw, 0);
  }

  /** The ceiling hides the plan from above, so it is only shown in first person. */
  private syncCeiling(): void {
    const ceiling = this.scene.getMeshByName("ceiling");
    if (ceiling) ceiling.isVisible = this.cameras.mode === "first-person";
  }

  dispose(): void {
    window.removeEventListener("resize", this.onResize);
    this.cameras.dispose();
    this.registry.disposeAll();
    this.furniture.dispose();
    this.materials.dispose();
    this.highlight.dispose();
    this.scene.dispose();
    this.engine.dispose();
  }
}
