import { Color3, type Material, type Scene, StandardMaterial, Texture } from "@babylonjs/core";
import type { BabylonSceneSpec, BoxMeshSpec } from "../types";

/** The landscape backdrop (public/map-end-background.jpg, 4678 x 3119): its horizon is 33.8% up from the bottom. */
const BACKDROP_ASPECT = 4678 / 3119;
const BACKDROP_HORIZON = 0.338;
/** Meters of sky covered by one copy of the sky photo (public/sky.avif); copies are mirrored, so no seams. */
const SKY_TILE = 40;

/**
 * One shared material per mesh kind (wall, header, sill, glass, floor, ceiling, ground), plus one per
 * scenery mesh with a photo (backdrop walls, sky), whose texture is scaled to that mesh's size.
 */
export class Materials {
  private readonly cache = new Map<string, StandardMaterial>();
  private readonly textures = new Map<string, Texture>();
  private scenery: Material[] = [];

  constructor(private readonly scene: Scene) {}

  get(kind: string, spec: BabylonSceneSpec): StandardMaterial {
    const existing = this.cache.get(kind);
    if (existing) return existing;
    const def = spec.materials[kind] ?? { color: "#cccccc", alpha: 1 };
    const mat = new StandardMaterial(`mat:${kind}`, this.scene);
    mat.diffuseColor = Color3.FromHexString(def.color);
    mat.specularColor = new Color3(0.05, 0.05, 0.05);
    const alpha = def.alpha ?? 1;
    if (alpha < 1) {
      mat.alpha = alpha;
      mat.backFaceCulling = false;
    }
    this.cache.set(kind, mat);
    return mat;
  }

  /** Material for a box: photo-textured for the boundary walls and the sky, else the shared one per kind. */
  forBox(box: BoxMeshSpec, spec: BabylonSceneSpec): Material {
    const def = spec.materials[box.kind];
    if (!def?.texture || (box.kind !== "boundary" && box.kind !== "sky")) return this.get(box.kind, spec);

    const texture = this.texture(def.texture).clone();
    const [width, height, depth] = box.size;
    if (box.kind === "boundary") {
      // The photo's horizon sits at the walker's eye level; its bottom (the near grass) is cut off
      // below the ground and its top reaches the wall's top. Repeats along the wall are mirrored.
      const eye = spec.camera.eyeHeight;
      const imageHeight = (height - eye) / (1 - BACKDROP_HORIZON);
      texture.vScale = height / imageHeight;
      texture.vOffset = 1 - texture.vScale;
      texture.uScale = width / (imageHeight * BACKDROP_ASPECT);
      texture.wrapU = Texture.MIRROR_ADDRESSMODE;
      texture.wrapV = Texture.CLAMP_ADDRESSMODE;
    } else {
      texture.uScale = width / SKY_TILE;
      texture.vScale = depth / SKY_TILE;
      texture.wrapU = Texture.MIRROR_ADDRESSMODE;
      texture.wrapV = Texture.MIRROR_ADDRESSMODE;
    }
    const mat = new StandardMaterial(`mat:${box.id}`, this.scene);
    mat.emissiveTexture = texture; // a photo: shown as is, not lit like the house
    mat.diffuseColor = Color3.Black();
    mat.specularColor = Color3.Black();
    mat.disableLighting = true;
    mat.backFaceCulling = false;
    this.scenery.push(mat);
    return mat;
  }

  private texture(url: string): Texture {
    let texture = this.textures.get(url);
    if (!texture) {
      texture = new Texture(url, this.scene, false, true, Texture.TRILINEAR_SAMPLINGMODE);
      this.textures.set(url, texture);
    }
    return texture;
  }

  /** Scenery materials belong to one loaded scene (their scale follows its size). */
  disposeScenery(): void {
    for (const mat of this.scenery) mat.dispose(false, true);
    this.scenery = [];
  }

  dispose(): void {
    this.disposeScenery();
    for (const mat of this.cache.values()) mat.dispose();
    this.cache.clear();
    for (const texture of this.textures.values()) texture.dispose();
    this.textures.clear();
  }
}
