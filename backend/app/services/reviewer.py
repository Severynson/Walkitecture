"""LLM review pass (ARCHITECTURE.md §4.3).

1. A deterministic *suspects list* (short/isolated walls, unsnapped openings, overlapping or homeless
   furniture, unnamed rooms, unidentified furniture blocks).
2. Suspects + warnings + stats (not the whole model) go to the LLM.
3. The LLM answers with patches from the review allowlist only.
4. Every patch runs through the edit engine: validated, applied one by one (each undoable, recorded
   with source="review"), or rejected with the engine's reasons.
"""

import json
import re
import uuid
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.schemas.base import CamelModel
from app.schemas.building import Building, FurnitureItem
from app.schemas.edits import REVIEW_OPERATIONS, ReviewCommand, ReviewCommandAdapter
from app.services import plan_store
from app.services.edits import EditRejected, apply_edit
from app.services.geometry import Problem, point_in_polygon, wall_connections
from app.services.llm import structured

SHORT_WALL = 0.3  # m
OVERLAP_SHARE = 0.3  # of the smaller footprint
MAX_SUSPECTS = 60
UNNAMED_ROOM = re.compile(r"^Room \d+$")


class Suspect(CamelModel):
    kind: Literal[
        "short_wall", "isolated_wall", "unsnapped_openings", "overlapping_furniture", "furniture_outside_rooms",
        "unlabelled_room", "unresolved_furniture_blocks",
    ]  # fmt: skip
    target_ids: list[str] = Field(default_factory=list)
    detail: str


class PatchResult(CamelModel):
    command: dict[str, Any] = Field(description="The allowlisted review command (as proposed)")
    reason: str
    status: Literal["applied", "valid", "rejected"]  # valid = would apply (preview mode)
    summary: str | None = None
    problems: list[Problem] = Field(default_factory=list)
    version: int | None = Field(default=None, description="Model version after this patch (applied only)")


class ReviewResult(CamelModel):
    suspects: list[Suspect]
    patches: list[PatchResult]
    notes: str
    applied: bool
    version: int


# --- 1. Suspects -------------------------------------------------------------------------


def _corners(item: FurnitureItem) -> list[tuple[float, float]]:
    (cx, cy), (fx, fy) = item.position, item.facing
    tx, ty = -fy, fx
    w, d = item.width / 2, item.depth / 2
    return [(cx + tx * sw * w + fx * sd * d, cy + ty * sw * w + fy * sd * d) for sw in (-1, 1) for sd in (-1, 1)]


def _aabb(item: FurnitureItem) -> tuple[float, float, float, float]:
    xs, ys = zip(*_corners(item), strict=True)
    return min(xs), min(ys), max(xs), max(ys)


def find_suspects(building: Building, warnings: list[str], stats: dict[str, Any] | None) -> list[Suspect]:
    suspects: list[Suspect] = []
    for wall in building.walls:
        if wall.length < SHORT_WALL:
            suspects.append(Suspect(kind="short_wall", target_ids=[wall.id], detail=f"{wall.length:.2f} m long"))
        elif not wall_connections(building, wall):
            suspects.append(
                Suspect(kind="isolated_wall", target_ids=[wall.id], detail=f"{wall.length:.2f} m, touches no wall")
            )
    for warning in warnings:
        if "not on any wall" in warning:
            suspects.append(Suspect(kind="unsnapped_openings", detail=warning))

    items = building.furniture
    for i, a in enumerate(items):
        ax0, ay0, ax1, ay1 = _aabb(a)
        for b in items[i + 1 :]:
            bx0, by0, bx1, by1 = _aabb(b)
            overlap = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))
            smaller = min(a.width * a.depth, b.width * b.depth)
            if smaller and overlap / smaller > OVERLAP_SHARE:
                suspects.append(
                    Suspect(
                        kind="overlapping_furniture", target_ids=[a.id, b.id],
                        detail=f"{a.category} and {b.category} overlap {overlap / smaller:.0%}",
                    )
                )  # fmt: skip
    polygons = [r.polygon for r in building.rooms if r.polygon]
    if polygons:
        for item in items:
            if not any(point_in_polygon(item.position, polygon) for polygon in polygons):
                suspects.append(
                    Suspect(kind="furniture_outside_rooms", target_ids=[item.id], detail=f"{item.category}")
                )
    for room in building.rooms:
        if UNNAMED_ROOM.match(room.name):
            suspects.append(Suspect(kind="unlabelled_room", target_ids=[room.id], detail=f"named {room.name!r}"))
    unresolved = (stats or {}).get("unresolvedFurnitureBlocks") or []
    if unresolved:
        suspects.append(Suspect(kind="unresolved_furniture_blocks", detail=", ".join(unresolved)))
    return suspects[:MAX_SUSPECTS]


def _object_summaries(building: Building, suspects: list[Suspect]) -> dict[str, Any]:
    ids = {i for s in suspects for i in s.target_ids}
    out: dict[str, Any] = {}
    for wall in building.walls:
        near = wall_connections(building, wall) if wall.id in ids else []
        if wall.id in ids:
            out[wall.id] = {
                "kind": "wall", "start": wall.start, "end": wall.end, "length": round(wall.length, 3),
                "thickness": wall.thickness, "connectedTo": near,
                "openings": [{"id": o.id, "type": o.type, "width": o.width} for o in wall.openings],
            }  # fmt: skip
            # Neighbours help the LLM decide merges.
            for other_id in near:
                other = building.find_wall(other_id)
                out.setdefault(other_id, {"kind": "wall", "start": other.start, "end": other.end})
    for item in building.furniture:
        if item.id in ids:
            out[item.id] = item.model_dump(mode="json", by_alias=True, include={"category", "model", "source_block",
                                                                             "position", "width", "depth"})  # fmt: skip
    for room in building.rooms:
        if room.id in ids:
            area = _polygon_area(room.polygon) if room.polygon else None
            out[room.id] = {"kind": "room", "name": room.name, "labelPosition": room.label_position, "area": area}
    out["_otherRoomNames"] = [r.name for r in building.rooms if r.id not in ids]
    return out


def _polygon_area(polygon: list[tuple[float, float]]) -> float:
    n = len(polygon)
    twice = sum(polygon[i][0] * polygon[(i + 1) % n][1] - polygon[(i + 1) % n][0] * polygon[i][1] for i in range(n))
    return round(abs(twice) / 2, 2)


# --- 2-3. LLM proposal ---------------------------------------------------------------------


class _Patch(BaseModel):
    """Flat schema for strict structured outputs; converted to a typed ReviewCommand by code."""

    operation: Literal[
        "delete_wall", "merge_walls", "set_opening_type", "delete_opening", "set_furniture_category",
        "delete_furniture", "rename_room",
    ]  # fmt: skip
    target_id: str | None
    wall_ids: list[str] | None
    opening_type: Literal["door", "window"] | None
    category: Literal["bed", "chair", "sofa", "desk", "table", "closet", "toilet"] | None
    name: str | None
    reason: str


class _Answer(BaseModel):
    patches: list[_Patch]
    notes: str


PROMPT = f"""You review a building model reconstructed from an architectural CAD drawing. You get a list of
deterministic *suspects* plus parser warnings and stats. Propose fixes ONLY with these operations:
{", ".join(REVIEW_OPERATIONS)}.
- delete_wall(target_id): stray fragments (e.g. very short isolated walls that are drawing artefacts).
- merge_walls(wall_ids): collinear pieces of one wall.
- set_opening_type(target_id, opening_type), delete_opening(target_id).
- set_furniture_category(target_id, category), delete_furniture(target_id): duplicates or misidentified items.
- rename_room(target_id, name): unnamed rooms, using nearby labels or what the room contains.
Be conservative: propose a patch only when the evidence is clear, never invent IDs, and give a one-line
reason. An empty list is a fine answer."""


def to_command(patch: _Patch) -> ReviewCommand:
    raw: dict[str, Any] = {"operation": patch.operation, "targetId": patch.target_id, "parameters": {}}
    if patch.operation == "merge_walls":
        raw = {"operation": "merge_walls", "parameters": {"wallIds": patch.wall_ids or []}}
    elif patch.operation == "set_opening_type":
        raw["parameters"] = {"type": patch.opening_type}
    elif patch.operation == "set_furniture_category":
        raw["parameters"] = {"category": patch.category}
    elif patch.operation == "rename_room":
        raw["parameters"] = {"name": patch.name}
    return ReviewCommandAdapter.validate_python(raw)


# --- 4. Run ---------------------------------------------------------------------------------


async def review_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    owner_id: uuid.UUID,
    settings: Settings,
    *,
    apply: bool,
    client: AsyncOpenAI | None = None,
) -> ReviewResult:
    plan = await plan_store.get_plan(session, plan_id, owner_id)
    building = plan_store.building_of(plan)
    warnings, stats, version = list(plan.parse_warnings or []), dict(plan.parse_stats or {}), plan.model_version
    await session.rollback()  # don't hold a transaction open during the LLM call
    stats.pop("layers", None)

    suspects = find_suspects(building, warnings, stats)
    if not suspects:
        return ReviewResult(suspects=[], patches=[], notes="No suspects found.", applied=apply, version=version)

    payload = {
        "suspects": [s.model_dump(by_alias=True) for s in suspects],
        "objects": _object_summaries(building, suspects),
        "warnings": warnings,
        "stats": stats,
    }
    answer = await structured(settings, PROMPT, json.dumps(payload, ensure_ascii=False), _Answer, client)

    results: list[PatchResult] = []
    preview = building
    for patch in answer.patches:
        raw = patch.model_dump(exclude={"reason"}, exclude_none=True)
        try:
            command = to_command(patch)
        except ValidationError as exc:
            problem = Problem(code="invalid_patch", message=exc.errors()[0]["msg"], target_id=patch.target_id)
            results.append(PatchResult(command=raw, reason=patch.reason, status="rejected", problems=[problem]))
            continue
        dumped = command.model_dump(mode="json", by_alias=True)
        try:
            if apply:
                edit = await plan_store.apply_and_record(
                    session, plan_id, owner_id, command, base_version=None, source="review"
                )
                version = edit.version
                results.append(
                    PatchResult(
                        command=dumped, reason=patch.reason, status="applied", summary=edit.summary, version=version
                    )
                )
            else:
                outcome = apply_edit(preview, command)
                preview = outcome.building  # later patches see earlier ones
                results.append(
                    PatchResult(command=dumped, reason=patch.reason, status="valid", summary=outcome.summary)
                )
        except EditRejected as exc:
            await session.rollback()
            results.append(PatchResult(command=dumped, reason=patch.reason, status="rejected", problems=exc.problems))
    return ReviewResult(suspects=suspects, patches=results, notes=answer.notes, applied=apply, version=version)
