// Aliases over the generated API contract (src/api/types.ts, from contracts/openapi.json).
import type { components } from "../api/types";

type Schemas = components["schemas"];

export type SceneResponse = Schemas["SceneResponse"];
export type BabylonSceneSpec = Schemas["BabylonScene"];
export type BoxMeshSpec = Schemas["BoxMesh"];
export type FurnitureSpec = Schemas["FurnitureSpec"];
export type RoomLabelSpec = Schemas["RoomLabel"];
export type Building = Schemas["Building"];

/** Every mesh the loader creates carries this in `mesh.metadata`. */
export interface MeshMeta {
  /** Object the mesh belongs to: a wall id, an opening id, a furniture id, or "building" (floor/ceiling). */
  objectId: string;
  kind: string;
  wallId?: string | null;
  openingId?: string | null;
}
