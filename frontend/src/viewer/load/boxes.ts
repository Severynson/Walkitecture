import { MeshBuilder, type Scene } from "@babylonjs/core";
import type { Registry } from "../registry";
import type { BabylonSceneSpec, BoxMeshSpec, MeshMeta } from "../types";
import type { Materials } from "./materials";

/** Kinds the first-person camera collides with. The ceiling and the sky are out of reach anyway. */
const SOLID = new Set(["wall", "header", "sill", "glass", "floor", "ground", "boundary"]);
/** First-person scenery around the building (not part of it): never picked, hidden in the top-down view. */
export const SCENERY = new Set(["ground", "boundary", "sky"]);
/** The map's end, for sightlines: hitting it means the view leaves the building. */
export const MAP_END = new Set(["boundary", "barrier", "sky"]);

/** Registry key of a box: its wall (so an edit to a wall rebuilds all its pieces) or "building". */
export function boxObjectId(box: BoxMeshSpec): string {
  return box.wallId ?? "building";
}

export function createBox(scene: Scene, box: BoxMeshSpec, spec: BabylonSceneSpec, materials: Materials, registry: Registry) {
  if (box.kind === "boundary") return createBackdrop(scene, box, spec, materials, registry);
  const [width, height, depth] = box.size;
  const mesh = MeshBuilder.CreateBox(box.id, { width, height, depth }, scene);
  mesh.position.set(box.position[0], box.position[1], box.position[2]);
  mesh.rotation.y = box.rotationY;
  mesh.material = materials.forBox(box, spec);
  mesh.checkCollisions = SOLID.has(box.kind);
  mesh.isPickable = box.kind !== "ceiling" && !SCENERY.has(box.kind);
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

/**
 * A boundary wall at the map's end: an invisible solid box (kind "barrier") stops the walker, and a plane
 * in its middle shows the backdrop photo. A plane, because a box's side faces don't share one texture
 * orientation (every other wall showed the field upside down).
 */
function createBackdrop(scene: Scene, box: BoxMeshSpec, spec: BabylonSceneSpec, materials: Materials, registry: Registry) {
  const [width, height, depth] = box.size;
  const barrier = MeshBuilder.CreateBox(`${box.id}:barrier`, { width, height, depth }, scene);
  barrier.position.set(box.position[0], box.position[1], box.position[2]);
  barrier.rotation.y = box.rotationY;
  barrier.isVisible = false;
  barrier.isPickable = false;
  barrier.checkCollisions = true;
  barrier.metadata = { objectId: box.id, kind: "barrier" } satisfies MeshMeta;
  barrier.freezeWorldMatrix();
  registry.add(boxObjectId(box), barrier);

  const plane = MeshBuilder.CreatePlane(box.id, { width, height }, scene);
  plane.position.set(box.position[0], box.position[1], box.position[2]);
  plane.rotation.y = box.rotationY;
  plane.material = materials.forBox(box, spec);
  plane.isPickable = false;
  plane.checkCollisions = false;
  plane.metadata = { objectId: box.id, kind: box.kind } satisfies MeshMeta;
  plane.freezeWorldMatrix();
  registry.add(boxObjectId(box), plane);
  return plane;
}
