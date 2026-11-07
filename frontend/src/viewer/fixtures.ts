import type { SceneResponse } from "./types";

// contracts/fixtures/*.scene.json — GET /api/plans/{id}/scene responses exported by
// backend/scripts/export_fixtures.py. Lazy: each fixture is its own chunk, fetched on demand.
const modules = import.meta.glob<SceneResponse>("../../../contracts/fixtures/*.scene.json", { import: "default" });

const byName = new Map(
  Object.entries(modules).map(([path, load]) => [path.split("/").pop()!.replace(/\.scene\.json$/, ""), load]),
);

export const FIXTURE_NAMES = [...byName.keys()].sort();

export async function loadFixture(name: string): Promise<SceneResponse> {
  const load = byName.get(name);
  if (!load) throw new Error(`Unknown fixture "${name}". Available: ${FIXTURE_NAMES.join(", ") || "none"}`);
  return load();
}
