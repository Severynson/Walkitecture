import type { Building, FurnitureItem, Opening, Wall } from "../api/schema";

export type OpeningSelection = { kind: "window" | "door"; id: string; wall: Wall; opening: Opening };

export type Selection =
  | OpeningSelection
  | { kind: "wall"; id: string; wall: Wall }
  | { kind: "furniture"; id: string; furniture: FurnitureItem };

/** Resolves a selected object id against the current model (null if it no longer exists). */
export function describe(model: Building, id: string | null): Selection | null {
  if (!id) return null;
  for (const wall of model.walls ?? []) {
    if (wall.id === id) return { kind: "wall", id, wall };
    for (const opening of wall.openings ?? []) {
      if (opening.id === id) return { kind: opening.type, id, wall, opening };
    }
  }
  const furniture = (model.furniture ?? []).find((f) => f.id === id);
  return furniture ? { kind: "furniture", id, furniture } : null;
}

export function wallLength(wall: Wall): number {
  return Math.hypot(wall.end[0] - wall.start[0], wall.end[1] - wall.start[1]);
}

const FOOT = 0.3048;

/** "1.20 m (3′ 11″)" */
export function metersAndFeet(meters: number): string {
  const totalInches = Math.round(meters / (FOOT / 12));
  const feet = Math.floor(totalInches / 12);
  const inches = totalInches % 12;
  return `${meters.toFixed(2)} m (${feet}′ ${inches}″)`;
}

export function titleCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1).replace(/_/g, " ");
}
