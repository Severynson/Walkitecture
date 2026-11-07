"""Apply allowlisted edit commands to a *copy* of the building model, then validate the result.

Edits never mutate the input model. Only geometry touched by the edit is validated, so imperfections
that came from an imported drawing do not block unrelated edits.
"""

from dataclasses import dataclass
from typing import NoReturn

from app.schemas.building import Building, Opening, Wall
from app.schemas.edits import (
    EditCommand,
    MoveSelectedWall,
    ResizeSelectedWindow,
    ResizeWindowParams,
    ResizeWindows,
    SetCeilingHeight,
)
from app.services.geometry import (
    MAX_CEILING_HEIGHT,
    MAX_WALL_MOVE,
    MIN_CEILING_HEIGHT,
    Problem,
    add,
    left_normal,
    scale,
    segment_distance,
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


def apply_edit(building: Building, command: EditCommand) -> EditResult:
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
