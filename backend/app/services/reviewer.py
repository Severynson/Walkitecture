"""LLM review pass (ARCHITECTURE.md §4.3, §4.4).

1. A deterministic *suspects list* (short/isolated walls, unsnapped openings, homeless furniture,
   unnamed rooms, unidentified furniture blocks), plus the guardrails' *findings* (furniture conflicts,
   interior windows, spaces without an entrance...), each with precomputed options.
2. Suspects, findings, warnings and stats (not the whole model) go to the LLM.
3. The LLM answers with patches from the review allowlist and one decision (an option id) per finding.
4. Every patch runs through the edit engine: validated, applied one by one, or rejected with the
   engine's reasons. During upload this happens before the model is stored (pipeline.py); the
   POST /review endpoint applies patches to a stored plan, each undoable.
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
from app.schemas.checks import CheckFinding
from app.schemas.edits import REVIEW_OPERATIONS, ReviewCommand, ReviewCommandAdapter
from app.schemas.parse import FurnitureCategory
from app.services.edits import EditRejected, apply_edit
from app.services.geometry import Problem, point_in_polygon, wall_connections
from app.services.llm import structured

SHORT_WALL = 0.3  # m
MAX_SUSPECTS = 60
UNNAMED_ROOM = re.compile(r"^Room \d+$")


class Suspect(CamelModel):
    kind: Literal[
        "short_wall", "isolated_wall", "unsnapped_openings", "furniture_outside_rooms", "unlabelled_room",
        "unresolved_furniture_blocks",
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
    category: FurnitureCategory | None
    name: str | None
    reason: str


class _Decision(BaseModel):
    finding_id: str
    option_id: str
    reason: str


class _Answer(BaseModel):
    patches: list[_Patch]
    decisions: list[_Decision]
    notes: str


class AiReview(BaseModel):
    """What the reviewer decided: typed patches (invalid ones dropped) and option ids per finding."""

    patches: list[ReviewCommand]
    decisions: dict[str, tuple[str, str]]  # finding id -> (option id, reason)
    rejected: list[str]
    notes: str


PROMPT = f"""You review a building model reconstructed from an architectural CAD drawing, before it is
shown as a walkable 3D scene. You get deterministic *suspects* and *findings*, parser warnings and stats.

1. Patches. Propose fixes for suspects ONLY with these operations: {", ".join(REVIEW_OPERATIONS)}.
- delete_wall(target_id): stray fragments (e.g. very short isolated walls that are drawing artefacts).
- merge_walls(wall_ids): collinear pieces of one wall.
- set_opening_type(target_id, opening_type), delete_opening(target_id).
- set_furniture_category(target_id, category), delete_furniture(target_id): duplicates or misidentified items
  (e.g. a symbol that is not furniture, an item whose category contradicts its block name or room).
- rename_room(target_id, name): unnamed rooms, using nearby labels or what the room contains.
Be conservative: propose a patch only when the evidence is clear, never invent IDs, one-line reason.

2. Decisions. Each finding breaks a rule the 3D scene must respect and lists precomputed options (all
geometrically valid) plus a default. Give exactly one decision per finding: its id and the id of the
option you choose, with a one-line reason. Think about where the mistake in the drawing or its
interpretation most likely is:
- furniture_conflict: furniture may never cut into walls or other furniture. The options differ in which
  piece moves/shrinks slightly or is removed. Keep the piece the drawing and room make plausible (a bed in a
  bedroom over an overlapping chair; one of two duplicate blocks); "accept" keeps the gentle fix.
- interior_window: a window between two indoor spaces is almost always a mistake: usually a door drawn or
  classified as a window (convert it, especially when a room behind it has no other entrance), sometimes a
  misread wall (remove it). Keep it only for a plausible glazed partition (offices) or a borrowed light.
- enclosed_space / no_exterior_door: a room nobody can enter is almost always a mistake: a door drawn as a
  window, a door leaf or threshold drawn on the wall layer closing the doorway, or a missing door. Choose the
  option that fixes the likely mistake; keep it only for shafts, ducts, light wells, or storage voids, or
  when the entrance is plausibly outside the drawing.
- facade_gap: a hole in the outer wall with no window or door is almost always a lost wall piece. Close it
  with the entrance door when the space behind is a hall or entrance (or the building has no other exterior
  door), with glazing when it is a living room or faces a terrace, with a plain wall otherwise; keep it open
  only for a real porch, terrace or open passage.
- furniture_outside: usually a drawing artefact (legend, symbol library, another sheet): remove it unless it
  plausibly stands on a balcony, terrace or garden.
Return empty lists when there is nothing to do."""


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


def _finding_view(finding: CheckFinding) -> dict[str, Any]:
    return {
        "id": finding.id, "kind": finding.kind, "detail": finding.detail, "targetIds": finding.target_ids,
        "options": [{"id": o.id, "description": o.description} for o in finding.options], "default": finding.default,
    }  # fmt: skip


async def ai_review(
    building: Building,
    warnings: list[str],
    stats: dict[str, Any] | None,
    findings: list[CheckFinding],
    settings: Settings,
    *,
    suspects: list[Suspect] | None = None,
    client: AsyncOpenAI | None = None,
) -> AiReview:
    """One LLM call for suspects and findings. Raises LLMUnavailable (callers fall back to defaults)."""
    stats = dict(stats or {})
    stats.pop("layers", None)
    suspects = find_suspects(building, warnings, stats) if suspects is None else suspects
    if not suspects and not findings:
        return AiReview(patches=[], decisions={}, rejected=[], notes="Nothing to review.")
    payload = {
        "suspects": [s.model_dump(by_alias=True) for s in suspects],
        "findings": [_finding_view(f) for f in findings],
        "objects": _object_summaries(building, suspects),
        "warnings": warnings,
        "stats": stats,
    }
    answer = await structured(settings, PROMPT, json.dumps(payload, ensure_ascii=False), _Answer, client)
    patches: list[ReviewCommand] = []
    rejected: list[str] = []
    for patch in answer.patches:
        try:
            patches.append(to_command(patch))
        except ValidationError as exc:
            rejected.append(f"{patch.operation} {patch.target_id}: {exc.errors()[0]['msg']}")
    options = {f.id: {o.id for o in f.options} for f in findings}
    decisions: dict[str, tuple[str, str]] = {}
    for decision in answer.decisions:
        if decision.option_id in options.get(decision.finding_id, ()):
            decisions[decision.finding_id] = (decision.option_id, decision.reason)
        else:
            rejected.append(f"decision {decision.finding_id} -> {decision.option_id}: not one of its options")
    return AiReview(patches=patches, decisions=decisions, rejected=rejected, notes=answer.notes)


async def review_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    owner_id: uuid.UUID,
    settings: Settings,
    *,
    apply: bool,
    client: AsyncOpenAI | None = None,
) -> ReviewResult:
    """POST /review on a stored plan: suspects only (findings are settled during upload)."""
    from app.services import plan_store  # plan_store -> pipeline -> reviewer: import at call time

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
        "findings": [],
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
