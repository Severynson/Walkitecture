import "@babylonjs/loaders/glTF";
import { type AssetContainer, LoadAssetContainerAsync, MeshBuilder, type Scene, TransformNode } from "@babylonjs/core";
import type { Registry } from "../registry";
import type { FurnitureSpec, MeshMeta } from "../types";

/** Collider height for furniture (the GLB meshes themselves don't collide, for performance). */
const COLLIDER_HEIGHT = 1.0;

/** Loads each GLB once per scene and instantiates it for every placement. */
export class FurnitureLibrary {
  private readonly containers = new Map<string, Promise<AssetContainer>>();

  constructor(private readonly scene: Scene) {}

  private container(url: string): Promise<AssetContainer> {
    let pending = this.containers.get(url);
    if (!pending) {
      pending = LoadAssetContainerAsync(url, this.scene).then((container) => {
        // Some library models have faces wound the wrong way (they render inside out): draw both sides.
        for (const material of container.materials) material.backFaceCulling = false;
        return container;
      });
      this.containers.set(url, pending);
    }
    return pending;
  }

  /** Places one furniture item as described by the backend's scene spec. Failures only log. */
  async place(item: FurnitureSpec, registry: Registry): Promise<void> {
    const meta: MeshMeta = { objectId: item.id, kind: "furniture" };

    // Invisible footprint collider: you bump into desks instead of walking through them.
    const [width, depth] = item.footprint;
    const collider = MeshBuilder.CreateBox(`${item.id}:collider`, { width, height: COLLIDER_HEIGHT, depth }, this.scene);
    collider.position.set(item.footprintCenter[0], COLLIDER_HEIGHT / 2, item.footprintCenter[2]);
    collider.rotation.y = Math.atan2(item.facing[0], item.facing[1]);
    collider.isVisible = false;
    collider.checkCollisions = true;
    collider.isPickable = false;
    collider.metadata = meta;
    registry.add(item.id, collider);

    let container: AssetContainer;
    try {
      container = await this.container(item.url);
    } catch (error) {
      console.warn(`Furniture model ${item.url} failed to load`, error);
      return;
    }
    if (collider.isDisposed()) return; // the scene was reloaded while the GLB was loading

    // Parent the glTF __root__ to a node carrying the spec's transform (the spec already
    // compensates for the loader's handedness conversion and the model's native size).
    const root = new TransformNode(item.id, this.scene);
    root.position.set(item.position[0], item.position[1], item.position[2]);
    root.rotation.y = item.rotationY;
    root.scaling.set(item.scaling[0], item.scaling[1], item.scaling[2]);
    root.metadata = meta;
    const entries = container.instantiateModelsToScene((name) => `${item.id}:${name}`, false);
    for (const node of entries.rootNodes) node.parent = root;
    for (const mesh of root.getChildMeshes()) {
      mesh.metadata = meta;
      mesh.isPickable = true;
      mesh.checkCollisions = false;
    }
    registry.add(item.id, root);
  }

  dispose(): void {
    for (const pending of this.containers.values()) pending.then((c) => c.dispose()).catch(() => {});
    this.containers.clear();
  }
}
