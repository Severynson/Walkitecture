import { ArcRotateCamera, type Scene, UniversalCamera, Vector3 } from "@babylonjs/core";
import type { BabylonSceneSpec } from "./types";

export type ViewMode = "first-person" | "top-down";

const WASD = { up: [87, 38], down: [83, 40], left: [65, 37], right: [68, 39] }; // W/S/A/D + arrows
const BODY_RADIUS = 0.3;

/** First-person walker (WASD + mouse, collisions, gravity) and an overhead orbit camera. */
export class Cameras {
  readonly walker: UniversalCamera;
  readonly overhead: ArcRotateCamera;
  mode: ViewMode = "first-person";

  constructor(
    private readonly scene: Scene,
    private readonly canvas: HTMLCanvasElement,
  ) {
    const walker = new UniversalCamera("walker", new Vector3(0, 1.6, 0), scene);
    walker.minZ = 0.05;
    walker.fov = 1.1;
    walker.speed = 0.09;
    walker.inertia = 0.6;
    walker.angularSensibility = 2500;
    walker.keysUp = WASD.up;
    walker.keysDown = WASD.down;
    walker.keysLeft = WASD.left;
    walker.keysRight = WASD.right;
    walker.applyGravity = true;
    walker.checkCollisions = true;
    this.walker = walker;

    const overhead = new ArcRotateCamera("overhead", -Math.PI / 2, 0.01, 30, Vector3.Zero(), scene);
    overhead.lowerBetaLimit = 0.01;
    overhead.upperBetaLimit = Math.PI / 2.2;
    overhead.wheelDeltaPercentage = 0.02;
    overhead.panningSensibility = 60;
    this.overhead = overhead;

    scene.activeCamera = walker;
    walker.attachControl(canvas, true);
  }

  /** Spawn point, body size, and overhead framing from the scene spec. */
  fit(spec: BabylonSceneSpec): void {
    const eye = spec.camera.eyeHeight;
    // Ellipsoid centered half an eye-height below the camera: feet on the floor, eyes at `eye`.
    this.walker.ellipsoid = new Vector3(BODY_RADIUS, eye / 2, BODY_RADIUS);
    this.walker.ellipsoidOffset = new Vector3(0, -eye / 2, 0);
    this.walker.position.set(spec.camera.position[0], spec.camera.position[1], spec.camera.position[2]);
    this.walker.setTarget(new Vector3(spec.camera.target[0], spec.camera.target[1], spec.camera.target[2]));

    const [x0, , z0] = spec.bounds.min;
    const [x1, y1, z1] = spec.bounds.max;
    this.overhead.target = new Vector3((x0 + x1) / 2, 0, (z0 + z1) / 2);
    this.overhead.radius = Math.max(x1 - x0, z1 - z0, 5) * 1.25 + y1;
    this.overhead.alpha = -Math.PI / 2;
    this.overhead.beta = 0.01;
    this.overhead.lowerRadiusLimit = 2;
    this.overhead.upperRadiusLimit = this.overhead.radius * 3;
  }

  setMode(mode: ViewMode): void {
    if (mode === this.mode) return;
    const from = mode === "top-down" ? this.walker : this.overhead;
    const to = mode === "top-down" ? this.overhead : this.walker;
    from.detachControl();
    this.scene.activeCamera = to;
    to.attachControl(this.canvas, true);
    if (mode === "top-down" && document.pointerLockElement) document.exitPointerLock();
    this.mode = mode;
  }

  toggle(): ViewMode {
    this.setMode(this.mode === "first-person" ? "top-down" : "first-person");
    return this.mode;
  }
}
