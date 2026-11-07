import { DynamicTexture, Mesh, MeshBuilder, type Scene, StandardMaterial } from "@babylonjs/core";
import type { Registry } from "../registry";
import type { RoomLabelSpec } from "../types";

const FONT_PX = 64;
const TEXT_HEIGHT_M = 0.28;

/** A camera-facing room name floating at eye height. */
export function createRoomLabel(scene: Scene, room: RoomLabelSpec, registry: Registry) {
  const font = `600 ${FONT_PX}px system-ui, sans-serif`;
  const probe = new DynamicTexture("label-probe", 64, scene);
  const ctx = probe.getContext();
  ctx.font = font;
  const textWidth = Math.ceil(ctx.measureText(room.name).width) + FONT_PX;
  probe.dispose();

  const texHeight = FONT_PX * 1.6;
  const texture = new DynamicTexture(`label:${room.id}`, { width: textWidth, height: texHeight }, scene, true);
  texture.hasAlpha = true;
  texture.drawText(room.name, FONT_PX / 2, FONT_PX * 1.15, font, "#2b2b2b", "rgba(255,255,255,0.82)", true);

  const material = new StandardMaterial(`label-mat:${room.id}`, scene);
  material.diffuseTexture = texture;
  material.emissiveTexture = texture;
  material.useAlphaFromDiffuseTexture = true;
  material.disableLighting = true;
  material.backFaceCulling = false;

  const plane = MeshBuilder.CreatePlane(
    `label:${room.id}`,
    { width: (TEXT_HEIGHT_M * textWidth) / texHeight, height: TEXT_HEIGHT_M },
    scene,
  );
  plane.position.set(room.position[0], room.position[1] + 0.6, room.position[2]);
  plane.billboardMode = Mesh.BILLBOARDMODE_ALL;
  plane.material = material;
  plane.isPickable = false;
  plane.metadata = { objectId: room.id, kind: "room_label" };
  plane.onDisposeObservable.addOnce(() => {
    material.dispose();
    texture.dispose();
  });
  registry.add(room.id, plane);
}
