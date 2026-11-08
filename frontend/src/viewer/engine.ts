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
import { boxObjectId, createBox, MAP_END, SCENERY } from "./load/boxes";
import { Doors } from "./load/doors";
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
const BLUEPRINT = new Color4(0.059, 0.239, 0.447, 1); // top-down background (--blueprint in styles.css)
const SKY = Color4.FromHexString("#5B93B5FF"); // first person: behind the sky photo's edges
const FELL_BELOW = -2; // m: a walker this far under the floor is put back at the spawn point
const OUT_OF_MAP = 0.5; // m beyond the map's boundary walls, likewise
const DOOR_REACH = 2.5; // m: a door this close under the crosshair can be opened with E
const FOCUS_EVERY = 6; // frames between door-focus checks
const UNSTICK_EVERY = 2; // frames between checks for colliders overlapping the walker
/** m a collider must reach into the walker's body before it is let go: it trapped the walker (a door swung shut
 * onto it, an edit put a wall where it stands). Walking along a wall or past a corner only touches the body. */
const EMBEDDED = 0.1;
/** m: a collider let go of collides again once it reaches no deeper than this (Babylon's own contact distance). */
const CLEAR = 0.03;
/** Never let go of these, even when overlapping the walker (it would fall through or leave the map). */
const ALWAYS_SOLID = new Set(["floor", "ground", "barrier", "boundary"]);

/** The door under the crosshair (first person, within reach), for the "Press E" hint. */
export interface DoorFocus {
  id: string;
  open: boolean;
}
/** Pick targets that are "the building" rather than an editable object. */
const NOT_SELECTABLE = new Set(["floor", "ceiling"]);

/**
 * How deep a box collider (wall piece, door leaf, furniture footprint) reaches into the walker's round body: its
 * radius minus the distance from the box to the nearest of the body's `axis` points (heights at x, z). <= 0: apart.
 */
function depthInto(mesh: AbstractMesh, x: number, z: number, axis: number[], radius: number): number {
  const box = mesh.getBoundingInfo().boundingBox;
  const { minimumWorld: lo, maximumWorld: hi, centerWorld: c } = box;
  if (x < lo.x - radius || x > hi.x + radius || z < lo.z - radius || z > hi.z + radius) return -radius;
  // The box's own axes (world matrix rows include scaling: normalize) and half extents along them.
  const axes = box.directions.map((d) => d.normalizeToNew());
  const half = axes.map((d) =>
    Math.max(...box.vectorsWorld.map((v) => Math.abs((v.x - c.x) * d.x + (v.y - c.y) * d.y + (v.z - c.z) * d.z))),
  );
  let nearest = Infinity;
  for (const y of axis) {
    let squared = 0;
    axes.forEach((d, i) => {
      const outside = Math.abs((x - c.x) * d.x + (y - c.y) * d.y + (z - c.z) * d.z) - half[i];
      if (outside > 0) squared += outside * outside;
    });
    nearest = Math.min(nearest, Math.sqrt(squared));
  }
  return radius - nearest;
}

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
  private readonly doors: Doors;
  private focusedDoor: DoorFocus | null = null;
  private frame = 0;
  /** Colliders let go of because they overlapped the walker (`freeTheWalker`). */
  private readonly ghosts = new Set<AbstractMesh>();
  /** Called when the door under the crosshair changes (or opens/closes). */
  onDoorFocus: ((focus: DoorFocus | null) => void) | null = null;
  private readonly highlight: HighlightLayer;
  private readonly selectionMaterial: StandardMaterial;
  private spec: BabylonSceneSpec | null = null;
  private selectedId: string | null = null;
  private readonly onResize = () => this.engine.resize();

  constructor(canvas: HTMLCanvasElement) {
    this.engine = new Engine(canvas, true, { preserveDrawingBuffer: false, stencil: true });
    const scene = new Scene(this.engine);
    scene.clearColor = SKY;
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
    this.doors = new Doors(scene);
    scene.onAfterRenderObservable.add(() => {
      if (++this.frame % FOCUS_EVERY === 0) this.updateDoorFocus();
    });

    scene.onBeforeRenderObservable.add(() => this.keepOnTheMap());
    scene.onBeforeRenderObservable.add(() => {
      if (this.frame % UNSTICK_EVERY === 0) this.freeTheWalker();
    });
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
    this.materials.disposeScenery();
    if (!keepCamera) this.doors.reset();
    this.spec = spec;
    for (const box of spec.meshes) createBox(this.scene, box, spec, this.materials, this.registry);
    for (const opening of spec.openings) if (opening.leaf) void this.doors.place(opening, this.registry);
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
    this.syncMode();
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
    for (const opening of spec.openings) {
      if (opening.leaf && affected.has(opening.wallId)) void this.doors.place(opening, this.registry);
    }
    for (const item of spec.furniture) if (affected.has(item.id)) void this.furniture.place(item, this.registry);
    this.syncMode();
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
    if (!meta || NOT_SELECTABLE.has(meta.objectId) || NOT_SELECTABLE.has(meta.kind) || SCENERY.has(meta.kind)) return null;
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

  /** Opens or closes a door (default: the one under the crosshair); returns its new state, or null. */
  toggleDoor(id: string | null = this.focusedDoor?.id ?? null): boolean | null {
    if (!id) return null;
    const open = this.doors.toggle(id);
    if (open !== null && this.focusedDoor?.id === id) this.setDoorFocus({ id, open });
    return open;
  }

  isDoorOpen(id: string): boolean {
    return this.doors.isOpen(id);
  }

  get doorFocus(): DoorFocus | null {
    return this.focusedDoor;
  }

  /** First person: the nearest thing under the crosshair; a door within reach gets the "Press E" hint. */
  private updateDoorFocus(): void {
    let focus: DoorFocus | null = null;
    if (this.cameras.mode === "first-person" && this.spec) {
      const camera = this.cameras.walker;
      const ray = new Ray(camera.position.clone(), camera.getDirection(Vector3.Forward()), DOOR_REACH);
      const target = (mesh: AbstractMesh) =>
        mesh.isEnabled() && !!mesh.metadata && (mesh.isVisible || mesh.name.endsWith(":doorway"));
      const hit = this.scene.pickWithRay(ray, target);
      const meta = hit?.pickedMesh?.metadata as MeshMeta | undefined;
      if (hit?.hit && meta?.kind === "door" && meta.openingId && this.doors.has(meta.openingId)) {
        focus = { id: meta.openingId, open: this.doors.isOpen(meta.openingId) };
      }
    }
    this.setDoorFocus(focus);
  }

  private setDoorFocus(focus: DoorFocus | null): void {
    if (focus?.id === this.focusedDoor?.id && focus?.open === this.focusedDoor?.open) return;
    this.focusedDoor = focus;
    this.onDoorFocus?.(focus);
  }

  setMode(mode: ViewMode): void {
    this.cameras.setMode(mode);
    this.syncMode();
  }

  toggleMode(): ViewMode {
    const mode = this.cameras.toggle();
    this.syncMode();
    return mode;
  }

  /**
   * Turns the walker toward the longest sightline that stays inside the building, so you don't spawn
   * staring at a wall — or out of an open front door at the sky (rays that hit nothing don't count).
   */
  private faceOpenSpace(): void {
    const origin = this.cameras.walker.position.clone();
    const solid = (mesh: AbstractMesh) => mesh.checkCollisions && mesh.isEnabled();
    // Sightline length in every direction (0 when the ray leaves the building: hits the map's end).
    const distances = Array.from({ length: SPAWN_RAYS }, (_, i) => {
      const yaw = (i / SPAWN_RAYS) * Math.PI * 2;
      const hit = this.scene.pickWithRay(new Ray(origin, new Vector3(Math.sin(yaw), 0, Math.cos(yaw)), MAX_SIGHTLINE), solid);
      const kind = (hit?.pickedMesh?.metadata as MeshMeta | undefined)?.kind;
      return hit?.hit && !SCENERY.has(kind ?? "") && !MAP_END.has(kind ?? "") ? hit.distance : 0;
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

  /**
   * The ceiling hides the plan from above, and the scenery (ground, backdrop walls, sky) would box it
   * in: both are only shown in first person, where the background is sky rather than blueprint.
   */
  private syncMode(): void {
    const firstPerson = this.cameras.mode === "first-person";
    for (const mesh of this.scene.meshes) {
      const kind = (mesh.metadata as MeshMeta | undefined)?.kind;
      if (kind === "ceiling" || (kind && SCENERY.has(kind))) mesh.isVisible = firstPerson;
    }
    this.scene.clearColor = firstPerson ? SKY : BLUEPRINT;
  }

  /**
   * Guardrail: the walker can always walk away. Babylon only stops the walker's own moves, so a collider that
   * ends up deep inside its body (a door swung shut onto it, an edit or undo that put a wall or furniture
   * where it stands, a spawn point too close to something) would pin it in place for good. Such a collider
   * stops colliding until the walker has stepped out of it. The floor and the map's end always stay solid.
   *
   * The body is round (Babylon's ellipsoid), so depth is measured from its axis: a square stand-in reaches
   * past the ellipsoid at its corners, and walking diagonally past a door jamb let go of the wall, which the
   * walker could then walk into (and, inside it, see through every wall around).
   */
  private freeTheWalker(): void {
    for (const mesh of this.ghosts) {
      if (mesh.isDisposed()) this.ghosts.delete(mesh);
    }
    if (this.cameras.mode !== "first-person") return;
    const walker = this.cameras.walker;
    const { ellipsoid, ellipsoidOffset, position } = walker;
    const radius = ellipsoid.x;
    // Babylon centers the collision ellipsoid at position - ellipsoid.y + ellipsoidOffset: sample the body's
    // axis a radius above the feet, at the middle, and a radius below the head.
    const middle = position.y - ellipsoid.y + ellipsoidOffset.y;
    const axis = [middle - ellipsoid.y + radius, middle, middle + ellipsoid.y - radius];
    for (const mesh of this.ghosts) {
      if (depthInto(mesh, position.x, position.z, axis, radius) <= CLEAR) {
        mesh.checkCollisions = true;
        this.ghosts.delete(mesh);
      }
    }
    for (const mesh of this.scene.meshes) {
      if (!mesh.checkCollisions || !mesh.isEnabled()) continue;
      const kind = (mesh.metadata as MeshMeta | undefined)?.kind;
      if (!kind || ALWAYS_SOLID.has(kind)) continue;
      if (depthInto(mesh, position.x, position.z, axis, radius) > EMBEDDED) {
        mesh.checkCollisions = false;
        this.ghosts.add(mesh);
      }
    }
  }

  /** Guardrail: whatever happens (a gap in a collider, a glitch), the walker never leaves the map. */
  private keepOnTheMap(): void {
    const spec = this.spec;
    if (!spec || this.cameras.mode !== "first-person") return;
    const { position } = this.cameras.walker;
    const [x0, , z0] = spec.mapBounds.min;
    const [x1, , z1] = spec.mapBounds.max;
    const off =
      position.y < FELL_BELOW ||
      position.x < x0 - OUT_OF_MAP ||
      position.x > x1 + OUT_OF_MAP ||
      position.z < z0 - OUT_OF_MAP ||
      position.z > z1 + OUT_OF_MAP;
    if (off) {
      const [x, y, z] = spec.camera.position;
      position.set(x, y, z);
      this.cameras.walker.cameraDirection.setAll(0);
    }
  }

  dispose(): void {
    window.removeEventListener("resize", this.onResize);
    this.cameras.dispose();
    this.registry.disposeAll();
    this.furniture.dispose();
    this.doors.dispose();
    this.materials.dispose();
    this.highlight.dispose();
    this.scene.dispose();
    this.engine.dispose();
  }
}
