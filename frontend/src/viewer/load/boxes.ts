import { MeshBuilder, type Scene } from "@babylonjs/core";
import type { Registry } from "../registry";
import type { BabylonSceneSpec, BoxMeshSpec, MeshMeta } from "../types";
import type { Materials } from "./materials";

/** Kinds the first-person camera collides with. The ceiling is out of reach anyway. */
const SOLID = new Set(["wall", "header", "sill", "glass", "floor"]);

/** Registry key of a box: its wall (so an edit to a wall rebuilds all its pieces) or "building". */
export function boxObjectId(box: BoxMeshSpec): string {
  return box.wallId ?? "building";
}

export function createBox(scene: Scene, box: BoxMeshSpec, spec: BabylonSceneSpec, materials: Materials, registry: Registry) {
  const [width, height, depth] = box.size;
  const mesh = MeshBuilder.CreateBox(box.id, { width, height, depth }, scene);
  mesh.position.set(box.position[0], box.position[1], box.position[2]);
  mesh.rotation.y = box.rotationY;
  mesh.material = materials.get(box.kind, spec);
  mesh.checkCollisions = SOLID.has(box.kind);
  mesh.isPickable = box.kind !== "ceiling";
  const meta: MeshMeta = {
    objectId: box.openingId ?? box.wallId ?? box.id,
    kind: box.kind,
    wallId: box.wallId,
    openingId: box.openingId,
  };
  mesh.metadata = meta;
  mesh.freezeWorldMatrix();
  registry.add(boxObjectId(box), mesh);
  return mesh;
}
