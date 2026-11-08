"""Apply allowlisted edit commands to a *copy* of the building model, then validate the result.

Edits never mutate the input model. Only geometry touched by the edit is validated, so imperfections
that came from an imported drawing do not block unrelated edits.
"""

import math
from dataclasses import dataclass
from typing import NoReturn

from app.config import FOOT
from app.schemas.building import Building, Opening, Point2, Wall
from app.schemas.edits import (
    AddOpening,
    AddWall,
    AnyCommand,
    DeleteFurniture,
    DeleteOpening,
    DeleteWall,
    MergeWalls,
    MoveSelectedWall,
    RenameRoom,
    ResizeSelectedWindow,
    ResizeWindowParams,
    ResizeWindows,
    SetCeilingHeight,
    SetFurnitureCategory,
    SetOpeningType,
    ShiftWall,
)
from app.services.furniture_catalog import choose_model
from app.services.geometry import (
    MAX_CEILING_HEIGHT,
    MAX_WALL_MOVE,
    MIN_CEILING_HEIGHT,
    MIN_HEADER,
    Problem,
    add,
    cross,
    dot,
    left_normal,
    point_segment_distance,
    scale,
    segment_distance,
    segments_intersect,
    sub,
    unit,
    validate_wall,
    wall_connections,
)

BUILDING_ID = "building"


class EditRejected(Exception):
    def __init__(self, problems: list[Problem]):
        self.problems = problems
        super().__init__("; ".join(p.message for p in problems))


@dataclass
class EditResult:
    building: Building
    affected_ids: list[str]
    summary: str


def apply_edit(building: Building, command: AnyCommand) -> EditResult:
    proposed = building.model_copy(deep=True)
    match command:
        case ResizeSelectedWindow():
            affected, summary = _resize_selected_window(proposed, command)
        case ResizeWindows():
            affected, summary = _resize_windows(proposed, command)
        case SetCeilingHeight():
            affected, summary = _set_ceiling_height(proposed, command)
        case MoveSelectedWall():
            affected, summary = _move_selected_wall(proposed, command)
        case DeleteWall():
            affected, summary = _delete_wall(proposed, command)
        case MergeWalls():
            affected, summary = _merge_walls(proposed, command)
        case SetOpeningType():
            affected, summary = _set_opening_type(proposed, command)
        case DeleteOpening():
            affected, summary = _delete_opening(proposed, command)
        case SetFurnitureCategory():
            affected, summary = _set_furniture_category(proposed, command)
        case DeleteFurniture():
            affected, summary = _delete_furniture(proposed, command)
        case RenameRoom():
            affected, summary = _rename_room(proposed, command)
        case AddOpening():
            affected, summary = _add_opening(proposed, command)
        case AddWall():
            affected, summary = _add_wall(proposed, command)
        case ShiftWall():
            affected, summary = _shift_wall(proposed, command)
        case _:  # pragma: no cover - the discriminated union makes this unreachable
            _reject("unsupported_operation", f"Unsupported operation: {command!r}")

    problems = _validate_affected(proposed, affected)
    if problems:
        raise EditRejected(problems)
    return EditResult(building=proposed, affected_ids=affected, summary=summary)


# --- Operations ----------------------------------------------------------------------


def _resize_selected_window(b: Building, cmd: ResizeSelectedWindow) -> tuple[list[str], str]:
    wall, window, note = _resolve_window(b, cmd.target_id)
    old_width = window.width
    shifted = _resize_window(wall, window, cmd.parameters)
    parts = [f"Window {window.id}: width {old_width:.2f} m -> {window.width:.2f} m"]
    if cmd.parameters.height is not None or cmd.parameters.sill_height is not None:
        parts.append(f"height {window.height:.2f} m, sill {window.sill_height:.2f} m")
    if shifted:
        parts.append("shifted to stay within the wall")
    if note:
        parts.append(note)
    return [window.id, wall.id], "; ".join(parts) + "."


def _resize_windows(b: Building, cmd: ResizeWindows) -> tuple[list[str], str]:
    params = ResizeWindowParams(factor=cmd.parameters.factor)
    affected: list[str] = []
    count = 0
    for wall in b.walls:
        windows = [o for o in wall.openings if o.type == "window"]
        for window in windows:
            _resize_window(wall, window, params)
            affected.append(window.id)
            count += 1
        if windows:
            affected.append(wall.id)
    if count == 0:
        _reject("no_windows", "The building has no windows to resize.")
    return affected, f"Resized {count} windows by a factor of {cmd.parameters.factor:g}."


def _set_ceiling_height(b: Building, cmd: SetCeilingHeight) -> tuple[list[str], str]:
    new_height = round(cmd.parameters.height, 4)
    if not MIN_CEILING_HEIGHT <= new_height <= MAX_CEILING_HEIGHT:
        _reject(
            "ceiling_height_out_of_range",
            f"Ceiling height {new_height:.2f} m is outside {MIN_CEILING_HEIGHT}-{MAX_CEILING_HEIGHT} m.",
        )
    old_height = b.ceiling_height
    affected = [BUILDING_ID]
    for wall in b.walls:
        # Full-height walls follow the ceiling; shorter walls (e.g. half walls) only get clamped.
        if abs(wall.height - old_height) < 1e-3 or wall.height > new_height:
            wall.height = new_height
            affected.append(wall.id)
    b.ceiling_height = new_height
    return affected, f"Ceiling height {old_height:.2f} m -> {new_height:.2f} m."


def _move_selected_wall(b: Building, cmd: MoveSelectedWall) -> tuple[list[str], str]:
    wall = b.find_wall(cmd.target_id)
    if wall is None:
        kind = b.kind_of(cmd.target_id)
        if kind is None:
            _reject("target_not_found", f"No object with ID {cmd.target_id}.", cmd.target_id)
        _reject("not_a_wall", f"{cmd.target_id} is a {kind}, not a wall. Select a wall to move.", cmd.target_id)

    offset = cmd.parameters.offset
    if abs(offset) < 1e-3:
        _reject("offset_too_small", "Move offset must be non-zero.", wall.id)
    if abs(offset) > MAX_WALL_MOVE:
        _reject("offset_too_large", f"Walls can move at most {MAX_WALL_MOVE} m at a time.", wall.id)

    connected = wall_connections(b, wall)
    if connected:
        _reject(
            "wall_not_independent",
            f"Wall {wall.id} is connected to {', '.join(connected)}. Only free-standing walls can be moved.",
            wall.id,
        )

    shift = scale(left_normal(wall.start, wall.end), offset)
    wall.start = _round_point(add(wall.start, shift))
    wall.end = _round_point(add(wall.end, shift))

    for other in b.walls:
        if other.id == wall.id:
            continue
        clearance = (wall.thickness + other.thickness) / 2
        if segment_distance(wall.start, wall.end, other.start, other.end) < clearance:
            _reject(
                "wall_collision",
                f"Moving wall {wall.id} by {offset:+.2f} m would collide with wall {other.id}.",
                wall.id,
            )

    affected = [wall.id, *(o.id for o in wall.openings)]
    return affected, f"Moved wall {wall.id} by {offset:+.2f} m along its normal. Room outlines were not updated."


# --- Review operations (ARCHITECTURE.md §4.3) ------------------------------------------

DOOR_HEIGHT = 7 * FOOT
WINDOW_SILL = 2.5 * FOOT
WINDOW_HEIGHT = 4 * FOOT
MERGE_PARALLEL_SIN = math.sin(math.radians(3.0))


def _wall_or_reject(b: Building, wall_id: str) -> Wall:
    wall = b.find_wall(wall_id)
    if wall is None:
        _reject("target_not_found", f"No wall with ID {wall_id}.", wall_id)
    return wall


def _delete_wall(b: Building, cmd: DeleteWall) -> tuple[list[str], str]:
    wall = _wall_or_reject(b, cmd.target_id)
    b.walls.remove(wall)
    gone = [o.id for o in wall.openings]
    note = f" and its openings {', '.join(gone)}" if gone else ""
    return [wall.id, *gone], f"Deleted wall {wall.id}{note}."


def _merge_walls(b: Building, cmd: MergeWalls) -> tuple[list[str], str]:
    ids = list(dict.fromkeys(cmd.parameters.wall_ids))
    if len(ids) < 2:
        _reject("merge_needs_two_walls", "merge_walls needs at least two different walls.")
    walls = [_wall_or_reject(b, wall_id) for wall_id in ids]
    base = walls[0]
    direction = unit(sub(base.end, base.start))
    reach = max(w.thickness for w in walls) / 2 + 0.05
    for wall in walls[1:]:
        parallel = abs(cross(direction, unit(sub(wall.end, wall.start)))) <= MERGE_PARALLEL_SIN
        on_line = all(abs(cross(direction, sub(p, base.start))) <= reach for p in (wall.start, wall.end))
        if not (parallel and on_line):
            _reject("walls_not_collinear", f"Walls {base.id} and {wall.id} are not collinear.", wall.id)
    ts = [dot(sub(p, base.start), direction) for w in walls for p in (w.start, w.end)]
    t0, t1 = min(ts), max(ts)
    start, end = add(base.start, scale(direction, t0)), add(base.start, scale(direction, t1))

    openings: list[Opening] = []
    for wall in walls:  # re-express every opening along the merged wall
        wall_dir = unit(sub(wall.end, wall.start))
        for opening in wall.openings:
            edges = [dot(sub(add(wall.start, scale(wall_dir, s)), start), direction)
                     for s in (opening.offset, opening.offset + opening.width)]  # fmt: skip
            openings.append(opening.model_copy(update={"offset": round(min(edges), 4)}))
    merged = Wall(
        id=base.id, start=_round_point(start), end=_round_point(end),
        thickness=max(w.thickness for w in walls), height=max(w.height for w in walls),
        openings=sorted(openings, key=lambda o: o.offset),
    )  # fmt: skip
    b.walls = [merged if w.id == base.id else w for w in b.walls if w.id not in ids[1:]]
    affected = [*ids, *(o.id for o in openings)]
    return affected, f"Merged walls {', '.join(ids)} into {base.id} ({merged.length:.2f} m)."


def _find_opening_or_reject(b: Building, opening_id: str) -> tuple[Wall, Opening]:
    found = b.find_opening(opening_id)
    if found is None:
        _reject("target_not_found", f"No door or window with ID {opening_id}.", opening_id)
    return found


def _set_opening_type(b: Building, cmd: SetOpeningType) -> tuple[list[str], str]:
    wall, opening = _find_opening_or_reject(b, cmd.target_id)
    new_type = cmd.parameters.type
    if opening.type == new_type:
        _reject("no_change", f"{opening.id} is already a {new_type}.", opening.id)
    top = wall.height - MIN_HEADER
    if new_type == "door":
        opening.sill_height, opening.height = 0.0, round(min(DOOR_HEIGHT, top), 4)
    else:
        opening.sill_height = round(min(WINDOW_SILL, top - 0.3), 4)
        opening.height = round(min(WINDOW_HEIGHT, top - opening.sill_height), 4)
    old = opening.type
    opening.type = new_type
    return [opening.id, wall.id], f"Changed {opening.id} from a {old} to a {new_type} (ID kept)."


def _delete_opening(b: Building, cmd: DeleteOpening) -> tuple[list[str], str]:
    wall, opening = _find_opening_or_reject(b, cmd.target_id)
    wall.openings.remove(opening)
    return [opening.id, wall.id], f"Deleted {opening.type} {opening.id} from wall {wall.id}."


def _furniture_or_reject(b: Building, furniture_id: str):
    item = next((f for f in b.furniture if f.id == furniture_id), None)
    if item is None:
        _reject("target_not_found", f"No furniture with ID {furniture_id}.", furniture_id)
    return item


def _set_furniture_category(b: Building, cmd: SetFurnitureCategory) -> tuple[list[str], str]:
    item = _furniture_or_reject(b, cmd.target_id)
    category = cmd.parameters.category
    model = choose_model(category, item.width, item.depth, name=item.source_block or "")
    if model is None:
        _reject("no_model", f"No library model for category {category!r}.", item.id)
    old = item.category
    item.category, item.model, item.height, item.matched_by = category, model.file, model.real_size[1], "manual"
    return [item.id], f"Changed {item.id} from {old} to {category} ({model.file})."


def _delete_furniture(b: Building, cmd: DeleteFurniture) -> tuple[list[str], str]:
    item = _furniture_or_reject(b, cmd.target_id)
    b.furniture.remove(item)
    return [item.id], f"Deleted {item.category} {item.id}."


def _rename_room(b: Building, cmd: RenameRoom) -> tuple[list[str], str]:
    room = next((r for r in b.rooms if r.id == cmd.target_id), None)
    if room is None:
        _reject("target_not_found", f"No room with ID {cmd.target_id}.", cmd.target_id)
    name = " ".join(cmd.parameters.name.split())
    if not name:
        _reject("empty_name", "Room names cannot be empty.", room.id)
    old, room.name = room.name, name
    return [room.id], f"Renamed {room.id} from {old!r} to {name!r}."


def _add_wall(b: Building, cmd: AddWall) -> tuple[list[str], str]:
    if b.kind_of(cmd.target_id) is not None:
        _reject("duplicate_id", f"ID {cmd.target_id} is already used.", cmd.target_id)
    params = cmd.parameters
    wall = Wall(
        id=cmd.target_id, start=_round_point(params.start), end=_round_point(params.end),
        thickness=round(params.thickness, 4), height=b.ceiling_height,
    )  # fmt: skip
    b.walls.append(wall)
    return [wall.id], f"Added wall {wall.id} ({wall.length:.2f} m)."


MAX_WALL_SHIFT = 1.0  # m


def _shift_wall(b: Building, cmd: ShiftWall) -> tuple[list[str], str]:
    """Move a wall along its normal together with its joints: walls meeting it at right angles (corners and
    T-junctions) stretch or shrink with it, and its own ends slide along the walls they rest on. Joints at any
    other angle, or a wall continuing in line, cannot follow, so the edit is rejected."""
    wall = _wall_or_reject(b, cmd.target_id)
    offset = cmd.parameters.offset
    if not 1e-3 <= abs(offset) <= MAX_WALL_SHIFT:
        _reject("offset_out_of_range", f"Walls shift by 0.001-{MAX_WALL_SHIFT} m.", wall.id)
    start, end = wall.start, wall.end
    d = unit(sub(end, start))
    shift = scale(left_normal(start, end), offset)
    joined: list[Wall] = []
    rests_on: list[tuple[Point2, Wall]] = []  # (new end of `wall`, the wall it rests on)
    for other in b.walls:
        if other is wall or other.length <= 0:
            continue
        tol = (wall.thickness + other.thickness) / 2 + 0.01
        ends = [k for k in ("start", "end") if point_segment_distance(getattr(other, k), start, end) <= tol]
        on = [p for p in (start, end) if point_segment_distance(p, other.start, other.end) <= tol]
        if not ends and not on and not segments_intersect(start, end, other.start, other.end):
            continue
        od = unit(sub(other.end, other.start))
        if abs(dot(od, d)) > 0.05:
            _reject("angled_joint", f"Wall {other.id} meets {wall.id} at an angle or in line.", wall.id)
        for k in ends:
            old = getattr(other, k)
            new = _round_point(add(old, shift))
            if k == "start":  # openings keep their place in the world: offsets count from the start
                moved = dot(sub(new, old), od)
                for opening in other.openings:
                    opening.offset = round(opening.offset - moved, 4)
            setattr(other, k, new)
        if ends:
            joined.append(other)
        rests_on += [(add(p, shift), other) for p in on]
    wall.start, wall.end = _round_point(add(start, shift)), _round_point(add(end, shift))
    for p, other in rests_on:
        if point_segment_distance(p, other.start, other.end) > (wall.thickness + other.thickness) / 2 + 0.01:
            _reject("joint_lost", f"Shifting {wall.id} would move its end off wall {other.id}.", wall.id)
    moved = {wall.id, *(w.id for w in joined), *(o.id for _, o in rests_on)}
    for other in b.walls:
        clearance = (wall.thickness + other.thickness) / 2
        if other.id not in moved and segment_distance(wall.start, wall.end, other.start, other.end) < clearance:
            _reject("wall_collision", f"Shifting wall {wall.id} by {offset:+.2f} m would hit wall {other.id}.", wall.id)
    affected = [wall.id, *(w.id for w in joined)]
    return affected, f"Shifted wall {wall.id} by {offset:+.2f} m" + (
        f" (stretching {', '.join(w.id for w in joined)})." if joined else "."
    )


def _add_opening(b: Building, cmd: AddOpening) -> tuple[list[str], str]:
    wall = _wall_or_reject(b, cmd.target_id)
    kind, params = cmd.parameters.type, cmd.parameters
    taken = {o.id for w in b.walls for o in w.openings}
    number = 1 + sum(1 for w in b.walls for o in w.openings if o.type == kind)
    while f"{kind}_{number}" in taken:
        number += 1
    top = wall.height - MIN_HEADER
    if kind == "door":
        sill, height = 0.0, min(DOOR_HEIGHT, top)
    else:
        sill = min(WINDOW_SILL, top - 0.3)
        height = min(WINDOW_HEIGHT, top - sill)
    opening = Opening(
        id=f"{kind}_{number}", type=kind, offset=round(params.offset, 4), width=round(params.width, 4),
        height=round(height, 4), sill_height=round(sill, 4),
    )  # fmt: skip
    wall.openings.append(opening)
    wall.openings.sort(key=lambda o: o.offset)
    return [opening.id, wall.id], f"Added {kind} {opening.id} ({opening.width:.2f} m) to wall {wall.id}."


# --- Helpers -------------------------------------------------------------------------


def _resolve_window(b: Building, target_id: str) -> tuple[Wall, Opening, str | None]:
    if found := b.find_opening(target_id):
        wall, opening = found
        if opening.type != "window":
            _reject("not_a_window", f"{target_id} is a {opening.type}, not a window.", target_id)
        return wall, opening, None
    if wall := b.find_wall(target_id):
        # Clicking the wall around a window is a common mis-pick; resolve it when unambiguous.
        windows = [o for o in wall.openings if o.type == "window"]
        if len(windows) == 1:
            return wall, windows[0], f"resolved wall {wall.id} to its only window"
        _reject(
            "not_a_window",
            f"{target_id} is a wall with {len(windows)} windows. Select a specific window.",
            target_id,
        )
    _reject("target_not_found", f"No window with ID {target_id}.", target_id)


def _resize_window(wall: Wall, window: Opening, params: ResizeWindowParams) -> bool:
    """Resize around the window's center; returns True if it had to shift to stay in the wall."""
    center = window.center
    new_width = window.width
    if params.factor is not None:
        new_width = window.width * params.factor
    elif params.width is not None:
        new_width = params.width
    new_offset = center - new_width / 2

    shifted = False
    length = wall.length
    if new_width <= length:
        if new_offset < 0:
            new_offset, shifted = 0.0, True
        elif new_offset + new_width > length:
            new_offset, shifted = length - new_width, True

    window.width = round(new_width, 4)
    window.offset = round(new_offset, 4)
    if params.height is not None:
        window.height = round(params.height, 4)
    if params.sill_height is not None:
        window.sill_height = round(params.sill_height, 4)
    return shifted


def _validate_affected(b: Building, affected: list[str]) -> list[Problem]:
    problems: list[Problem] = []
    affected_set = set(affected)
    for wall in b.walls:
        if wall.id in affected_set:
            problems.extend(validate_wall(wall))
    return problems


def _round_point(p: tuple[float, float]) -> tuple[float, float]:
    return (round(p[0], 4), round(p[1], 4))


def _reject(code: str, message: str, target_id: str | None = None) -> NoReturn:
    raise EditRejected([Problem(code=code, message=message, target_id=target_id)])
