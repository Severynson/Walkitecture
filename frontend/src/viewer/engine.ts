import { Color3, Color4, DirectionalLight, Engine, HemisphericLight, Scene, Vector3 } from "@babylonjs/core";
import { Cameras, type ViewMode } from "./cameras";
import { boxObjectId, createBox } from "./load/boxes";
import { FurnitureLibrary } from "./load/furniture";
import { createRoomLabel } from "./load/labels";
import { Materials } from "./load/materials";
import { Registry } from "./registry";
import type { BabylonSceneSpec } from "./types";

const GRAVITY = -9.81 / 60; // per frame at 60 fps

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
  private spec: BabylonSceneSpec | null = null;
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

    this.cameras = new Cameras(scene, canvas);
    this.materials = new Materials(scene);
    this.furniture = new FurnitureLibrary(scene);

    this.engine.runRenderLoop(() => scene.render());
    window.addEventListener("resize", this.onResize);
  }

  /** Replaces whatever is shown with `spec` and moves the walker to the spawn point. */
  load(spec: BabylonSceneSpec): void {
    this.registry.disposeAll();
    this.spec = spec;
    for (const box of spec.meshes) createBox(this.scene, box, spec, this.materials, this.registry);
    for (const room of spec.rooms) createRoomLabel(this.scene, room, this.registry);
    for (const item of spec.furniture) void this.furniture.place(item, this.registry);
    this.cameras.fit(spec);
    this.syncCeiling();
  }

  /**
   * Applies a new spec after an edit without moving the camera. `affectedIds` are wall / opening /
   * furniture ids; "building" (global changes such as the ceiling height) rebuilds everything.
   */
  applyUpdate(spec: BabylonSceneSpec, affectedIds: string[]): void {
    if (!this.spec || affectedIds.includes("building")) {
      const position = this.cameras.walker.position.clone();
      const rotation = this.cameras.walker.rotation.clone();
      this.load(spec);
      this.cameras.walker.position.copyFrom(position);
      this.cameras.walker.rotation.copyFrom(rotation);
      return;
    }
    const affected = new Set(affectedIds);
    // Openings belong to walls: rebuild the whole wall an affected opening sits in.
    for (const opening of spec.openings) if (affected.has(opening.id)) affected.add(opening.wallId);
    for (const id of affected) this.registry.dispose(id);
    this.spec = spec;
    for (const box of spec.meshes) {
      if (affected.has(boxObjectId(box))) createBox(this.scene, box, spec, this.materials, this.registry);
    }
    for (const item of spec.furniture) if (affected.has(item.id)) void this.furniture.place(item, this.registry);
    this.syncCeiling();
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

  /** The ceiling hides the plan from above, so it is only shown in first person. */
  private syncCeiling(): void {
    const ceiling = this.scene.getMeshByName("ceiling");
    if (ceiling) ceiling.isVisible = this.cameras.mode === "first-person";
  }

  dispose(): void {
    window.removeEventListener("resize", this.onResize);
    this.registry.disposeAll();
    this.furniture.dispose();
    this.materials.dispose();
    this.scene.dispose();
    this.engine.dispose();
  }
}
