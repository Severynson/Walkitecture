import "@babylonjs/loaders/glTF";
import {
  type AssetContainer,
  LoadAssetContainerAsync,
  MeshBuilder,
  type Observer,
  type Scene,
  TransformNode,
} from "@babylonjs/core";
import type { components } from "../../api/types";
import type { Registry } from "../registry";
import type { MeshMeta } from "../types";

type OpeningSpec = components["schemas"]["OpeningSpec"];

/** Seconds a door takes to open or close. */
const SWING_SECONDS = 0.45;

interface DoorState {
  pivot: TransformNode;
  closed: number;
  swing: number;
  open: boolean;
}

/**
 * Hinged door leaves (scene spec `openings[].leaf`): a pivot node on the hinge carries the GLB leaf and an
 * invisible collider, so the walker bumps into a closed door and walks through an open one.
 * `toggle` swings a door open or shut; open doors stay open across edits (`load(…, {keepCamera})`).
 */
export class Doors {
  private readonly containers = new Map<string, Promise<AssetContainer>>();
  private readonly doors = new Map<string, DoorState>();
  private readonly openIds = new Set<string>();
  private readonly animator: Observer<Scene>;

  constructor(private readonly scene: Scene) {
    this.animator = scene.onBeforeRenderObservable.add(() => this.animate());
  }

  private container(url: string): Promise<AssetContainer> {
    let pending = this.containers.get(url);
    if (!pending) {
      pending = LoadAssetContainerAsync(url, this.scene).then((container) => {
        for (const material of container.materials) material.backFaceCulling = false;
        return container;
      });
      this.containers.set(url, pending);
    }
    return pending;
  }

  /** Builds one leaf; registered under its wall so rebuilding the wall rebuilds the door. */
  async place(opening: OpeningSpec, registry: Registry): Promise<void> {
    const leaf = opening.leaf;
    if (!leaf) return;
    const meta: MeshMeta = { objectId: opening.id, kind: "door", wallId: opening.wallId, openingId: opening.id };
    const pivot = new TransformNode(`${opening.id}:pivot`, this.scene);
    pivot.position.set(leaf.hinge[0], leaf.hinge[1], leaf.hinge[2]);
    const open = this.openIds.has(opening.id);
    pivot.rotation.y = leaf.rotationY + (open ? leaf.swing : 0);
    pivot.metadata = meta;
    registry.add(opening.wallId, pivot);
    this.doors.set(opening.id, { pivot, closed: leaf.rotationY, swing: leaf.swing, open });

    // The doorway itself (invisible, not solid): looking through an open door still targets it.
    const wall = this.wallThickness(opening);
    const doorway = MeshBuilder.CreateBox(
      `${opening.id}:doorway`,
      { width: opening.width, height: opening.height, depth: wall },
      this.scene,
    );
    doorway.position.set(opening.position[0], opening.position[1], opening.position[2]);
    doorway.rotation.y = opening.rotationY;
    doorway.isVisible = false;
    doorway.isPickable = false;
    doorway.checkCollisions = false;
    doorway.metadata = meta;
    registry.add(opening.wallId, doorway);

    const [width, height, thickness] = leaf.size;
    const collider = MeshBuilder.CreateBox(`${opening.id}:collider`, { width, height, depth: thickness }, this.scene);
    collider.parent = pivot;
    collider.position.set(width / 2, height / 2, 0);
    collider.isVisible = false;
    collider.checkCollisions = true;
    collider.isPickable = false;
    collider.metadata = meta;

    let container: AssetContainer;
    try {
      container = await this.container(leaf.url);
    } catch (error) {
      console.warn(`Door model ${leaf.url} failed to load`, error);
      return;
    }
    if (pivot.isDisposed()) return; // rebuilt while loading
    const root = new TransformNode(`${opening.id}:leaf`, this.scene);
    root.parent = pivot;
    root.position.set(leaf.modelPosition[0], leaf.modelPosition[1], leaf.modelPosition[2]);
    root.rotation.y = leaf.modelRotationY;
    root.scaling.set(leaf.scaling[0], leaf.scaling[1], leaf.scaling[2]);
    const entries = container.instantiateModelsToScene((name) => `${opening.id}:door:${name}`, false);
    for (const node of entries.rootNodes) node.parent = root;
    for (const mesh of root.getChildMeshes()) {
      mesh.metadata = meta;
      mesh.isPickable = true;
      mesh.checkCollisions = false;
    }
  }

  /** Depth of the doorway target: the wall's thickness (from its boxes), at least the leaf's. */
  private wallThickness(opening: OpeningSpec): number {
    const header = this.scene.getMeshByName(`${opening.wallId}:header:${opening.id}`);
    const depth = header?.getBoundingInfo().boundingBox.extendSize.z;
    return Math.max(depth ? depth * 2 : 0.2, 0.1);
  }

  has(id: string): boolean {
    const door = this.doors.get(id);
    return !!door && !door.pivot.isDisposed();
  }

  isOpen(id: string): boolean {
    return this.doors.get(id)?.open ?? false;
  }

  /** Opens a closed door or closes an open one; returns the new state (null: no such door). */
  toggle(id: string): boolean | null {
    const door = this.doors.get(id);
    if (!door || door.pivot.isDisposed()) return null;
    door.open = !door.open;
    if (door.open) this.openIds.add(id);
    else this.openIds.delete(id);
    return door.open;
  }

  /** A fresh scene: every door starts closed. */
  reset(): void {
    this.doors.clear();
    this.openIds.clear();
  }

  private animate(): void {
    const step = (this.scene.getEngine().getDeltaTime() / 1000 / SWING_SECONDS) * Math.PI * 0.5;
    for (const door of this.doors.values()) {
      if (door.pivot.isDisposed()) continue;
      const target = door.closed + (door.open ? door.swing : 0);
      const delta = target - door.pivot.rotation.y;
      if (Math.abs(delta) < 1e-4) continue;
      door.pivot.rotation.y += Math.sign(delta) * Math.min(Math.abs(delta), step);
    }
  }

  dispose(): void {
    this.scene.onBeforeRenderObservable.remove(this.animator);
    for (const pending of this.containers.values()) pending.then((c) => c.dispose()).catch(() => {});
    this.containers.clear();
    this.doors.clear();
  }
}
