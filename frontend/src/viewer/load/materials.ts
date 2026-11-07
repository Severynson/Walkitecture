import { Color3, type Scene, StandardMaterial } from "@babylonjs/core";
import type { BabylonSceneSpec } from "../types";

/** One shared material per mesh kind (wall, header, sill, glass, floor, ceiling). */
export class Materials {
  private readonly cache = new Map<string, StandardMaterial>();

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

  dispose(): void {
    for (const mat of this.cache.values()) mat.dispose();
    this.cache.clear();
  }
}
