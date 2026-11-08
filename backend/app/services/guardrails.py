"""Guardrails: what the 3D scene may never show, checked and fixed before it is built (ARCHITECTURE.md §4.4).

Hard rule, always enforced by code (`settle`):
* Furniture never intersects a wall (doorways included), the clear floor in front of a door (both sides,
  `door_clearances`), or another piece of furniture. A conflicting item moves (up to MAX_SHIFT) and/or
  shrinks a little (down to MIN_SCALE) to the nearest clear spot, or is removed when there is no room for
  it. Only a chair tucked under a table or desk may overlap it (up to CHAIR_TUCK). Bigger, more important
  pieces are placed first and keep their spot.

Judgement calls (`analyze`): found by code, decided by the AI among precomputed options (each one a
validated edit, so the AI can choose but never invent geometry), with a safe default without AI:
* which piece gives way in a furniture conflict;
* furniture standing outside the building;
* windows between two indoor spaces (almost always a door drawn as a window, or a misread wall);
* spaces that no door leads into, and buildings without an exterior door.

`resolve` applies the AI's review patches and decisions, then runs `settle` again, so the hard rule
holds whatever was decided. All of this is pure CPU work on a Building (run in a worker process).
"""

import heapq
import math
import re
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations, product

from app.schemas.building import Building, Footprint, FurnitureItem, Point2, Wall
from app.schemas.checks import CheckChange, CheckFinding, CheckOption
from app.schemas.edits import AnyCommand, AnyCommandAdapter
from app.services.babylon_scene import MAX_JOINT_GAP, SIZE_RANGE, rendered_footprint, wall_spans
from app.services.edits import EditRejected, apply_edit
from app.services.furniture_catalog import CATALOG
from app.services.geometry import (
    SegmentGrid,
    add,
    cross,
    dot,
    left_normal,
    line_intersection,
    point_segment_distance,
    project_param,
    scale,
    segments_intersect,
    sub,
    unit,
)

# --- Furniture rule -----------------------------------------------------------------------------

TOUCH = 0.01  # m of overlap tolerated everywhere (rounding, models touching a wall)
CHAIR_TUCK = 0.2  # m a chair may sit under a table or desk
MAX_SHIFT = 0.35  # m: furniture moves at most this far from where the drawing put it
SHIFT_STEP = 0.025
SCALE_STEPS = (1.0, 0.95, 0.9, 0.85)  # "downscale a little": never below MIN_SCALE
MIN_SCALE = SCALE_STEPS[-1]
SCALE_COST = 2.0  # m of movement that shrinking to 0% would be worth: 5% smaller ~ 10 cm moved
MTV_ITERATIONS = 8
# Who keeps their spot in a conflict: fixtures and big pieces first, chairs last.
CATEGORY_RANK = {
    "bed": 6, "bathtub": 6, "shower": 6, "kitchen": 6, "fridge": 6, "sink": 6, "toilet": 5, "closet": 5, "sofa": 5,
    "table": 4, "desk": 4, "bookshelf": 4, "dresser": 4, "cabinet": 3, "nightstand": 2, "chair": 1,
}  # fmt: skip
_REAL_SIZE = {m.file: m.real_size for m in CATALOG}


@dataclass(frozen=True, slots=True)
class Box:
    """Oriented rectangle: center, unit axis u (half extent hu) and v = u rotated +90 deg (half extent hv)."""

    cx: float
    cy: float
    ux: float
    uy: float
    hu: float
    hv: float

    def radius(self, ax: float, ay: float) -> float:
        """Half length of the projection onto the unit axis (ax, ay)."""
        return self.hu * abs(self.ux * ax + self.uy * ay) + self.hv * abs(self.ux * ay - self.uy * ax)

    def bbox(self) -> tuple[float, float, float, float]:
        rx, ry = self.radius(1.0, 0.0), self.radius(0.0, 1.0)
        return (self.cx - rx, self.cy - ry, self.cx + rx, self.cy + ry)

    def moved(self, dx: float, dy: float, k: float = 1.0) -> "Box":
        return Box(self.cx + dx, self.cy + dy, self.ux, self.uy, self.hu * k, self.hv * k)


def penetration(a: Box, b: Box) -> tuple[float, Point2]:
    """How deep `a` and `b` overlap (<= 0: apart) and the unit direction that pushes `a` out of `b`
    (separating axis theorem)."""
    dx, dy = a.cx - b.cx, a.cy - b.cy
    best: tuple[float, Point2] = (math.inf, (0.0, 0.0))
    for ax, ay in ((a.ux, a.uy), (-a.uy, a.ux), (b.ux, b.uy), (-b.uy, b.ux)):
        distance = dx * ax + dy * ay
        overlap = a.radius(ax, ay) + b.radius(ax, ay) - abs(distance)
        if overlap < best[0]:
            sign = 1.0 if distance >= 0 else -1.0
            best = (overlap, (ax * sign, ay * sign))
    return best


def furniture_box(item: FurnitureItem) -> Box:
    """The footprint the model is rendered with (babylon_scene.rendered_footprint), centered on the item."""
    width, depth = rendered_footprint(item)
    fx, fy = item.facing
    return Box(item.position[0], item.position[1], -fy, fx, width / 2, depth / 2)


def wall_boxes(walls: list[Wall]) -> list[tuple[Wall, Box]]:
    """Every wall as rendered: full thickness, ends extended into the walls they meet, doorways included."""
    out = []
    for wall, (s0, s1) in zip(walls, wall_spans(walls), strict=True):
        if wall.length <= 0:
            continue
        ux, uy = unit(sub(wall.end, wall.start))
        mid = (s0 + s1) / 2
        out.append(
            (wall, Box(wall.start[0] + ux * mid, wall.start[1] + uy * mid, ux, uy, (s1 - s0) / 2, wall.thickness / 2))
        )
    return out


LEAF_DOOR_MAX = 1.2  # m: doors up to this wide get a hinged leaf (doors.SINGLE_DOOR_MAX)
PASSAGE_CLEARANCE = 0.8  # m kept free on each side of a wider door (double door, open passage)


def door_clearances(walls: list[Wall]) -> list[tuple[str, Box]]:
    """The floor that must stay free in front of every door, on both sides: as wide as the door and as deep
    as it is wide (the leaf sweeps a quarter circle that fits in this square, whichever way it opens), or
    PASSAGE_CLEARANCE deep for doors too wide for a leaf. Each box spans the wall's thickness too."""
    out = []
    for wall in walls:
        if wall.length <= 0:
            continue
        ux, uy = unit(sub(wall.end, wall.start))
        for opening in wall.openings:
            if opening.type != "door":
                continue
            depth = opening.width if opening.width <= LEAF_DOOR_MAX else PASSAGE_CLEARANCE
            s = opening.offset + opening.width / 2
            box = Box(wall.start[0] + ux * s, wall.start[1] + uy * s, ux, uy, opening.width / 2,
                      wall.thickness / 2 + depth)  # fmt: skip
            out.append((f"doorway {opening.id}", box))
    return out


class _WallIndex:
    """Everything furniture may not cut into besides other furniture: walls as rendered and door clearances."""

    def __init__(self, walls: list[Wall], extra: list[tuple[str, Box]] = ()):
        self.items = [(wall.id, box) for wall, box in wall_boxes(walls)] + door_clearances(walls) + list(extra)
        segments = []
        for _, box in self.items:
            dx, dy = box.ux * box.hu, box.uy * box.hu
            segments.append(((box.cx - dx, box.cy - dy), (box.cx + dx, box.cy + dy)))
        self.max_half = max((box.hv for _, box in self.items), default=0.0)
        self.grid = SegmentGrid(segments, cell=2.0)

    def near(self, bbox: tuple[float, float, float, float], margin: float) -> list[tuple[str, Box]]:
        return [self.items[i] for i in self.grid.near(*bbox, margin=margin + self.max_half)]


class _FurnitureIndex:
    CELL = 2.0

    def __init__(self) -> None:
        self.cells: dict[tuple[int, int], list[tuple[FurnitureItem, Box]]] = defaultdict(list)

    def _keys(self, x0: float, y0: float, x1: float, y1: float):
        for gx in range(math.floor(x0 / self.CELL), math.floor(x1 / self.CELL) + 1):
            for gy in range(math.floor(y0 / self.CELL), math.floor(y1 / self.CELL) + 1):
                yield (gx, gy)

    def add(self, item: FurnitureItem, box: Box) -> None:
        for key in self._keys(*box.bbox()):
            self.cells[key].append((item, box))

    def near(self, bbox: tuple[float, float, float, float], margin: float) -> list[tuple[FurnitureItem, Box]]:
        x0, y0, x1, y1 = bbox
        seen: dict[str, tuple[FurnitureItem, Box]] = {}
        for key in self._keys(x0 - margin, y0 - margin, x1 + margin, y1 + margin):
            for item, box in self.cells.get(key, ()):
                seen.setdefault(item.id, (item, box))
        return list(seen.values())


def _allowance(a: FurnitureItem, b: FurnitureItem) -> float:
    pair = {a.category, b.category}
    return CHAIR_TUCK if "chair" in pair and pair & {"table", "desk"} else TOUCH


def _hits(item: FurnitureItem, box: Box, walls, furniture) -> list[tuple[str, float]]:
    """(id, depth) of every wall / settled item that `box` cuts into beyond what is allowed."""
    out = [(wall_id, depth) for wall_id, other in walls if (depth := penetration(box, other)[0]) > TOUCH]
    for other_item, other in furniture:
        if other_item.id != item.id and (depth := penetration(box, other)[0]) > _allowance(item, other_item):
            out.append((other_item.id, depth))
    return out


def _search_offsets() -> list[tuple[float, float, float]]:
    """(dx, dy, scale) candidates, cheapest first: small moves before shrinking, never beyond the limits."""
    n = round(MAX_SHIFT / SHIFT_STEP)
    out = []
    for i, j in product(range(-n, n + 1), repeat=2):
        dx, dy = i * SHIFT_STEP, j * SHIFT_STEP
        distance = math.hypot(dx, dy)
        if distance <= MAX_SHIFT + 1e-9:
            out.extend((distance + SCALE_COST * (1 - k), dx, dy, k) for k in SCALE_STEPS)
    out.sort()
    return [(dx, dy, k) for _, dx, dy, k in out]


SEARCH = _search_offsets()


def _min_scale(item: FurnitureItem) -> float:
    """Shrinking stops at MIN_SCALE, and before the model would fall below its smallest believable size."""
    real = _REAL_SIZE.get(item.model)
    if real is None:
        return MIN_SCALE
    width, depth = rendered_footprint(item)
    size = math.sqrt((width / real[0]) * (depth / real[2]))
    return max(MIN_SCALE, SIZE_RANGE[0] / size + 1e-9)


def _place(item: FurnitureItem, walls: _WallIndex, placed: _FurnitureIndex) -> tuple[float, float, float] | None:
    """The cheapest (dx, dy, scale) that clears every wall and settled item, or None."""
    base = furniture_box(item)
    reach = MAX_SHIFT + 0.05
    near_walls = walls.near(base.bbox(), reach)
    near_items = placed.near(base.bbox(), reach)
    obstacles = [(other, TOUCH) for _, other in near_walls]
    obstacles += [(other, _allowance(item, other_item)) for other_item, other in near_items if other_item.id != item.id]

    # 1. Push straight out of the deepest overlap: usually the exact smallest move.
    dx = dy = 0.0
    for _ in range(MTV_ITERATIONS):
        box = base.moved(dx, dy)
        worst: tuple[float, Point2] | None = None
        for other, allowed in obstacles:
            depth, direction = penetration(box, other)
            if depth > allowed and (worst is None or depth - allowed > worst[0]):
                worst = (depth - allowed, direction)
        if worst is None:
            if math.hypot(dx, dy) <= MAX_SHIFT:
                return (dx, dy, 1.0)
            break
        dx, dy = dx + worst[1][0] * (worst[0] + 0.002), dy + worst[1][1] * (worst[0] + 0.002)

    # 2. The nearest clear spot on a grid around the drawn position, allowing a little shrinking.
    lowest = _min_scale(item)
    for dx, dy, k in SEARCH:
        if k < lowest:
            continue
        box = base.moved(dx, dy, k)
        if all(penetration(box, other)[0] <= allowed for other, allowed in obstacles):
            return (dx, dy, k)
    return None


@dataclass
class FurnitureFix:
    item_id: str
    category: str
    action: str  # moved | resized | removed
    shift: float
    scale: float
    conflicts: list[str]
    summary: str
    decided: bool = False  # removed because a review decision asked for it


def _priority(item: FurnitureItem) -> tuple[int, float]:
    width, depth = rendered_footprint(item)
    return (CATEGORY_RANK.get(item.category, 2), width * depth)


def _adjusted(item: FurnitureItem, dx: float, dy: float, k: float) -> FurnitureItem:
    drawn = item.drawn or Footprint(position=item.position, width=item.width, depth=item.depth)
    update: dict = {"position": (round(item.position[0] + dx, 4) + 0.0, round(item.position[1] + dy, 4) + 0.0)}
    if k < 1:
        width, depth = rendered_footprint(item)  # store the rendered size: rendered_footprint is idempotent
        update |= {"width": round(width * k, 4), "depth": round(depth * k, 4)}
    update["drawn"] = drawn
    return item.model_copy(update=update)


def _fix_summary(item: FurnitureItem, dx: float, dy: float, k: float, conflicts: list[str]) -> tuple[str, str]:
    what = f"{item.category} {item.id}"
    clear = f"to clear {', '.join(conflicts[:3])}"
    shift = math.hypot(dx, dy)
    if k < 1:
        moved = f" and moved it {shift:.2f} m" if shift > 0.005 else ""
        return "resized", f"Shrank {what} to {k:.0%}{moved} {clear}."
    return "moved", f"Moved {what} {shift:.2f} m {clear}."


def settle(
    building: Building,
    remove: set[str] | frozenset[str] = frozenset(),
    walkways: list[tuple[str, Box]] = (),
) -> tuple[Building, list[FurnitureFix]]:
    """Enforce the furniture rule (`walkways`: floor that must stay clear too, see `make_walkable`): returns the
    corrected building (input untouched) and what changed."""
    walls = _WallIndex(building.walls, walkways)
    placed = _FurnitureIndex()
    kept: dict[str, FurnitureItem] = {}
    fixes: list[FurnitureFix] = []
    for item in sorted(building.furniture, key=_priority, reverse=True):
        if item.id in remove:
            fixes.append(
                FurnitureFix(item.id, item.category, "removed", 0.0, 1.0, [],
                             f"Removed {item.category} {item.id} (review decision).", decided=True)
            )  # fmt: skip
            continue
        box = furniture_box(item)
        hits = _hits(item, box, walls.near(box.bbox(), 0.05), placed.near(box.bbox(), 0.05))
        if not hits:
            kept[item.id] = item
            placed.add(item, box)
            continue
        conflicts = list(dict.fromkeys(hit_id for hit_id, _ in sorted(hits, key=lambda h: -h[1])))
        found = _place(item, walls, placed)
        if found is None:
            summary = f"Removed {item.category} {item.id}: no room for it clear of {', '.join(conflicts[:3])}."
            fixes.append(FurnitureFix(item.id, item.category, "removed", 0.0, 1.0, conflicts, summary))
            continue
        dx, dy, k = found
        adjusted = _adjusted(item, dx, dy, k)
        kept[item.id] = adjusted
        placed.add(adjusted, furniture_box(adjusted))
        action, summary = _fix_summary(item, dx, dy, k, conflicts)
        fixes.append(FurnitureFix(item.id, item.category, action, round(math.hypot(dx, dy), 4), k, conflicts, summary))
    furniture = [kept[f.id] for f in building.furniture if f.id in kept]
    return building.model_copy(update={"furniture": furniture}), fixes


def find_conflicts(building: Building, walkways: list[tuple[str, Box]] = ()) -> list[tuple[str, str, float]]:
    """(furniture id, wall/furniture id, depth) for every violation of the furniture rule."""
    walls = _WallIndex(building.walls, walkways)
    index = _FurnitureIndex()
    boxes = [(item, furniture_box(item)) for item in building.furniture]
    for item, box in boxes:
        index.add(item, box)
    furniture_ids = {item.id for item, _ in boxes}
    out: list[tuple[str, str, float]] = []
    pairs: set[frozenset[str]] = set()
    for item, box in boxes:
        for hit_id, depth in _hits(item, box, walls.near(box.bbox(), 0.05), index.near(box.bbox(), 0.05)):
            if hit_id in furniture_ids:
                if frozenset((item.id, hit_id)) in pairs:
                    continue  # each furniture pair once
                pairs.add(frozenset((item.id, hit_id)))
            out.append((item.id, hit_id, round(depth, 4)))
    return out


# --- Spaces: rooms, doors, outdoors -------------------------------------------------------------

REGION_CELL = 0.1  # m: raster resolution for finding rooms
MAX_REGION_CELLS = 160_000  # coarser cells for big drawings ...
MAX_REGION_CELL = 0.25  # ... and no space checks beyond this (rooms would blur together)
OPEN_DIRECTIONS = 3  # of 8: a free cell connected to the border is outdoors when this many rays escape
MIN_SPACE_AREA = 1.5  # m²: smaller pockets (shafts, wall cavities, columns) are not rooms
MIN_SPACE_WIDTH = 0.6  # m: narrower strips (the cavity of a double wall) are not rooms either
LEAKED_BUILDING = 4.0  # m² of enclosed floor reachable from outside: the building has an open entrance
DOOR_WIDTH = 0.8
WIDE_DOOR_WIDTH = 0.9
END_CLEARANCE = 0.15  # m between a new door and the end of its wall
MAX_DOOR_CANDIDATES = 3
MAX_CONVERTIBLE_WINDOW = 2.0  # m: wider "windows" are not turned into doors
PLUG_WALL = (0.5, 1.5)  # m: a wall this long closing a room may be a door leaf drawn on the wall layer
OUTSIDE_HULL_MARGIN = 0.3  # m beyond the building's convex outline before furniture counts as outside
OUTSIDE_FURNITURE_GAP = 1.5  # m from any wall: outdoor furniture this far out is removed by default
CIRCULATION = re.compile(
    r"hall|corridor|passage|lobby|foyer|entr|living|lounge|pasillo|distrib|recib|vest[ií]bulo|sal[oó]n|estar",
    re.IGNORECASE,
)


@dataclass
class _Regions:
    """Raster of the plan: walls (-1) and connected free areas (labels >= 0); `outdoor` marks free
    cells outside the building (see `_regions`)."""

    x0: float
    y0: float
    cell: float
    nx: int
    ny: int
    labels: array
    outdoor: bytearray
    areas: dict[int, float]
    widths: dict[int, float]  # mean width (area / longest extent): thin strips are wall cavities, not rooms
    exterior: int  # label of the free space around the building
    leaked_area: float  # m² of the exterior label that is enclosed on most sides: a building open to it
    names: dict[int, list[str]] = field(default_factory=dict)

    def is_space(self, label: int) -> bool:
        """Big and wide enough to be a room (not a shaft, a column, or the cavity of a double wall)."""
        return self.areas.get(label, 0) >= MIN_SPACE_AREA and self.widths.get(label, 0) >= MIN_SPACE_WIDTH

    def index(self, p: Point2) -> int | None:
        i, j = math.floor((p[0] - self.x0) / self.cell), math.floor((p[1] - self.y0) / self.cell)
        if 0 <= i < self.nx and 0 <= j < self.ny:
            return j * self.nx + i
        return None

    def probe(self, point: Point2, direction: Point2, start: float) -> int | None:
        """First free cell from `point` along `direction`, beginning `start` m out (past a wall face)."""
        for k in range(6):
            idx = self.index(add(point, scale(direction, start + self.cell * (k + 0.5))))
            if idx is None:
                return None
            if self.labels[idx] >= 0:
                return idx
        return None

    def name(self, label: int) -> str:
        names = self.names.get(label)
        return " / ".join(dict.fromkeys(names)) if names else f"an unnamed {self.areas.get(label, 0):.1f} m² space"


def _rasterize(boxes: list[Box], x0: float, y0: float, cell: float, nx: int, ny: int, grow: float) -> bytearray:
    blocked = bytearray(nx * ny)
    for box in boxes:
        bx0, by0, bx1, by1 = box.bbox()
        i0, i1 = max(0, math.floor((bx0 - grow - x0) / cell)), min(nx - 1, math.floor((bx1 + grow - x0) / cell))
        j0, j1 = max(0, math.floor((by0 - grow - y0) / cell)), min(ny - 1, math.floor((by1 + grow - y0) / cell))
        hu, hv = box.hu + grow, box.hv + grow
        for j in range(j0, j1 + 1):
            py = y0 + (j + 0.5) * cell - box.cy
            row = j * nx
            for i in range(i0, i1 + 1):
                px = x0 + (i + 0.5) * cell - box.cx
                if abs(px * box.ux + py * box.uy) <= hu and abs(py * box.ux - px * box.uy) <= hv:
                    blocked[row + i] = 1
    return blocked


def _neighbors(idx: int, nx: int, n: int):
    i = idx % nx
    if i > 0:
        yield idx - 1
    if i < nx - 1:
        yield idx + 1
    if idx >= nx:
        yield idx - nx
    if idx + nx < n:
        yield idx + nx


def _regions(building: Building, boxes: list[Box]) -> _Regions | None:
    points = [p for w in building.walls for p in (w.start, w.end)]
    if not points or not boxes:
        return None
    points += [f.position for f in building.furniture]  # stray furniture far out must be on the raster too
    pad = 1.0
    x0, y0 = min(p[0] for p in points) - pad, min(p[1] for p in points) - pad
    x1, y1 = max(p[0] for p in points) + pad, max(p[1] for p in points) + pad
    cell = max(REGION_CELL, math.sqrt((x1 - x0) * (y1 - y0) / MAX_REGION_CELLS))
    if cell > MAX_REGION_CELL:
        return None
    nx, ny = math.ceil((x1 - x0) / cell), math.ceil((y1 - y0) / cell)
    n = nx * ny
    walls = _rasterize(boxes, x0, y0, cell, nx, ny, cell / 2)  # half a cell: thin walls stay watertight

    labels = array("i", [-2]) * n
    areas: dict[int, float] = {}
    widths: dict[int, float] = {}
    for idx in range(n):
        if walls[idx]:
            labels[idx] = -1
    for seed in range(n):
        if labels[seed] != -2:
            continue
        label = len(areas)
        labels[seed] = label
        stack, count = [seed], 0
        i0 = i1 = seed % nx
        j0 = j1 = seed // nx
        while stack:
            idx = stack.pop()
            count += 1
            i, j = idx % nx, idx // nx
            i0, i1, j0, j1 = min(i0, i), max(i1, i), min(j0, j), max(j1, j)
            for nb in _neighbors(idx, nx, n):
                if labels[nb] == -2:
                    labels[nb] = label
                    stack.append(nb)
        areas[label] = count * cell * cell
        widths[label] = count * cell / max(i1 - i0 + 1, j1 - j0 + 1)

    # Outdoors: free space connected to the drawing's border (which also leaks into a building through
    # an open facade or a wide gap) AND open to the sky in at least OPEN_DIRECTIONS of 8 directions
    # (deep inside such a leaky room, walls still surround you on most sides).
    exterior = labels[0]
    open_count = bytearray(n)
    for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
        free = bytearray(n)  # 1: a ray from this cell in (di, dj) reaches the border without a wall
        irange = range(nx - 1, -1, -1) if di > 0 else range(nx)
        for j in range(ny - 1, -1, -1) if dj > 0 else range(ny):
            nj = j + dj
            row, nrow = j * nx, nj * nx
            inside_j = 0 <= nj < ny
            for i in irange:
                ni = i + di
                if not inside_j or not 0 <= ni < nx:
                    free[row + i] = 1
                elif free[nrow + ni] and not walls[nrow + ni]:
                    free[row + i] = 1
        for idx in range(n):
            open_count[idx] += free[idx]
    outdoor = bytearray(n)
    leaked = 0
    for idx in range(n):
        if labels[idx] == exterior:
            if open_count[idx] >= OPEN_DIRECTIONS:
                outdoor[idx] = 1
            elif open_count[idx] <= 1:
                leaked += 1

    regions = _Regions(x0, y0, cell, nx, ny, labels, outdoor, areas, widths, exterior, leaked * cell * cell)
    for room in building.rooms:
        if room.label_position and (idx := regions.index(room.label_position)) is not None and labels[idx] >= 0:
            regions.names.setdefault(labels[idx], []).append(room.name)
    return regions


def _wall_frame(wall: Wall) -> tuple[Point2, Point2]:
    d = unit(sub(wall.end, wall.start))
    return d, (-d[1], d[0])


def _sides(regions: _Regions, wall: Wall, s: float) -> tuple[int | None, int | None]:
    """Free cells on the left and right of `wall` at `s` m from its start."""
    d, n = _wall_frame(wall)
    p = add(wall.start, scale(d, s))
    half = wall.thickness / 2
    return regions.probe(p, n, half), regions.probe(p, (-n[0], -n[1]), half)


@dataclass
class _Spaces:
    regions: _Regions
    space_of: dict[int, int]  # region label -> component root (spaces >= MIN_SPACE_AREA, plus outdoors)
    outside: int  # component root of the outdoors
    main: int  # component of the building's main part


OUTDOORS = -10  # pseudo label of the outdoors in the space graph


def _space_graph(building: Building, regions: _Regions) -> _Spaces:
    parent: dict[int, int] = {OUTDOORS: OUTDOORS}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    touches_outdoors: set[int] = set()
    for idx, label in enumerate(regions.labels):
        if label >= 0 and regions.outdoor[idx]:
            touches_outdoors.add(label)
    for label in regions.areas:
        if regions.is_space(label) or label in touches_outdoors:
            parent[label] = label
    for label in touches_outdoors:
        union(label, OUTDOORS)

    def node(idx: int | None) -> int | None:
        if idx is None:
            return None
        if regions.outdoor[idx]:
            return OUTDOORS
        label = regions.labels[idx]
        return label if label in parent else None

    for wall in building.walls:
        for opening in wall.openings:
            if opening.type != "door":
                continue
            a, b = (node(idx) for idx in _sides(regions, wall, opening.offset + opening.width / 2))
            if a is not None and b is not None:
                union(a, b)

    components: dict[int, float] = defaultdict(float)
    for label in parent:
        if label != OUTDOORS:
            components[find(label)] += regions.areas[label]
    outside = find(OUTDOORS)
    # The building is reachable from outside through a door, or through an open entrance (the space
    # around it reaches into enclosed floor area).
    reachable = regions.leaked_area >= LEAKED_BUILDING or any(
        find(label) == outside and label not in touches_outdoors for label in parent if label != OUTDOORS
    )
    if reachable or not components:
        main = outside
    else:  # no door leads outdoors: the biggest connected part is "the building"
        main = max((root for root in components if root != outside), key=lambda r: components[r], default=outside)
    return _Spaces(regions, {label: find(label) for label in parent}, outside, main)


def _component_labels(spaces: _Spaces, root: int) -> set[int]:
    return {label for label, r in spaces.space_of.items() if r == root and label != OUTDOORS}


# --- Walkable paths -----------------------------------------------------------------------------

PATH_WIDTH = 0.9  # m of clear floor between the doors of every room and hallway (the walker is 0.6 m wide)
WALKER_RADIUS = 0.3  # m: narrower than this, nobody gets through at all
PATH_BOX_STEP = 1  # raster cells between the boxes that keep a found path clear (1: turns stay covered)
WALKWAY_ROUNDS = 3
MAX_WIDEN = 0.6  # m: a wall moves at most this far to widen a passage


def _rasterize_round(boxes: list[Box], x0: float, y0: float, cell: float, nx: int, ny: int, grow: float) -> bytearray:
    """Cells whose center lies within `grow` of a box (round corners, unlike `_rasterize`)."""
    blocked = bytearray(nx * ny)
    g2 = grow * grow
    for box in boxes:
        bx0, by0, bx1, by1 = box.bbox()
        i0, i1 = max(0, math.floor((bx0 - grow - x0) / cell)), min(nx - 1, math.floor((bx1 + grow - x0) / cell))
        j0, j1 = max(0, math.floor((by0 - grow - y0) / cell)), min(ny - 1, math.floor((by1 + grow - y0) / cell))
        for j in range(j0, j1 + 1):
            py = y0 + (j + 0.5) * cell - box.cy
            row = j * nx
            for i in range(i0, i1 + 1):
                px = x0 + (i + 0.5) * cell - box.cx
                du = max(abs(px * box.ux + py * box.uy) - box.hu, 0.0)
                dv = max(abs(py * box.ux - px * box.uy) - box.hv, 0.0)
                if du * du + dv * dv <= g2:
                    blocked[row + i] = 1
    return blocked


def _solid_wall_boxes(walls: list[Wall]) -> list[Box]:
    """Walls as rendered with their doorways open (windows stay solid)."""
    out = []
    for wall, (s0, s1) in zip(walls, wall_spans(walls), strict=True):
        if wall.length <= 0:
            continue
        ux, uy = unit(sub(wall.end, wall.start))
        at, pieces = s0, []
        for lo, hi in sorted((o.offset, o.offset + o.width) for o in wall.openings if o.type == "door"):
            if lo > at:
                pieces.append((at, lo))
            at = max(at, hi)
        if s1 > at:
            pieces.append((at, s1))
        for a, b in pieces:
            if b - a > 1e-3:
                mid = (a + b) / 2
                out.append(
                    Box(wall.start[0] + ux * mid, wall.start[1] + uy * mid, ux, uy, (b - a) / 2, wall.thickness / 2)
                )
    return out


@dataclass
class _Floor:
    """Where a walker may go in each room: rooms are the free areas between walls (doors closed, `_regions`)."""

    regions: _Regions
    solid: bytearray  # walls, doorways open
    body: bytearray  # closer to a wall than WALKER_RADIUS (outside door clearances): nobody fits
    tight: bytearray  # closer to a wall than PATH_WIDTH / 2 (outside door clearances)
    furniture: bytearray  # closer to furniture than PATH_WIDTH / 2 (outside door clearances): it must move
    blocked: bytearray  # so close to furniture that moving it MAX_SHIFT is not enough: it would go
    entries: dict[int, list[tuple[str, int]]]  # room label -> (door id, cell just inside that door)


def _floor(building: Building) -> _Floor | None:
    regions = _regions(building, [box for _, box in wall_boxes(building.walls)])
    if regions is None:
        return None
    grid = (regions.x0, regions.y0, regions.cell, regions.nx, regions.ny)
    half = PATH_WIDTH / 2
    walls = _solid_wall_boxes(building.walls)
    slots = _rasterize([box for _, box in door_clearances(building.walls)], *grid, 0.0)

    def outside_slots(cells: bytearray) -> bytearray:
        return bytearray(c and not s for c, s in zip(cells, slots, strict=True))

    entries: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for wall in building.walls:
        if wall.length <= 0:
            continue
        d, n = _wall_frame(wall)
        for o in (o for o in wall.openings if o.type == "door"):
            mid = add(wall.start, scale(d, o.offset + o.width / 2))
            depth = o.width if o.width <= LEAF_DOOR_MAX else PASSAGE_CLEARANCE
            reach = wall.thickness / 2 + min(depth, half)
            for side in (1.0, -1.0):
                idx = regions.index(add(mid, scale(n, side * reach)))
                if idx is None or regions.outdoor[idx] or regions.labels[idx] < 0:
                    continue
                if regions.is_space(label := regions.labels[idx]):
                    entries[label].append((o.id, idx))
    boxes = [furniture_box(i) for i in building.furniture]
    return _Floor(
        regions,
        solid=_rasterize(walls, *grid, 0.0),
        body=outside_slots(_rasterize_round(walls, *grid, WALKER_RADIUS)),
        tight=outside_slots(_rasterize_round(walls, *grid, half)),
        furniture=outside_slots(_rasterize_round(boxes, *grid, half - 2 * TOUCH)),  # `settle` allows TOUCH
        blocked=outside_slots(_rasterize_round(boxes, *grid, max(half - MAX_SHIFT, 0.0))),
        entries=entries,
    )


REMOVAL_COST = 25  # a path cell that only removing a piece of furniture clears costs this many pushed aside


def _cheapest_path(floor: _Floor, sources: set[int], target: int, cost) -> list[int] | None:
    """Cheapest way from any source to `target` (Dijkstra); cost(idx) is a small int, or None (no way)."""
    nx, n = floor.regions.nx, floor.regions.nx * floor.regions.ny
    dist = dict.fromkeys(sources, 0)
    prev: dict[int, int] = {}
    heap = [(0, idx) for idx in sources]
    heapq.heapify(heap)
    while heap:
        d, idx = heapq.heappop(heap)
        if d > dist[idx]:
            continue
        if idx == target:
            path = [idx]
            while path[-1] in prev:
                path.append(prev[path[-1]])
            return path[::-1]
        for nb in _neighbors(idx, nx, n):
            step = cost(nb)
            if step is None or dist.get(nb, math.inf) <= d + step:
                continue
            dist[nb], prev[nb] = d + step, idx
            heapq.heappush(heap, (d + step, nb))
    return None


@dataclass
class _Narrow:
    """Two doors of a room that only a passage narrower than PATH_WIDTH (between walls) connects."""

    label: int
    doors: tuple[str, str]
    cells: list[int]  # the too-narrow cells on the best way between them


def _paths(building: Building) -> tuple[list[tuple[str, Box]], list[_Narrow], _Floor | None]:
    """Every room's doors must be connected by a PATH_WIDTH wide clear path. Where furniture blocks the way:
    boxes along the way that blocks the fewest of it (to keep clear, `settle`); where walls do: the passage."""
    floor = _floor(building)
    if floor is None:
        return [], [], None
    labels, cell = floor.regions.labels, floor.regions.cell
    walkways: list[tuple[str, Box]] = []
    narrow: list[_Narrow] = []
    for label, entries in sorted(floor.entries.items()):
        if len({door for door, _ in entries}) < 2:
            continue

        def walls_only(idx: int, label: int = label) -> int | None:
            return None if labels[idx] != label or floor.solid[idx] or floor.tight[idx] else 0

        def with_furniture(idx: int, label: int = label) -> int | None:
            # squeezing past furniture (it moves a little) beats going through it (it is removed)
            if walls_only(idx) is None:
                return None
            return REMOVAL_COST if floor.blocked[idx] else floor.furniture[idx]

        def squeeze(idx: int, label: int = label) -> int | None:
            return None if labels[idx] != label or floor.solid[idx] or floor.body[idx] else floor.tight[idx]

        first, start = entries[0]
        reached = {start}
        opened: set[int] = set()
        for door, idx in entries[1:]:
            if idx in reached or door == first:
                continue
            clear = _cheapest_path(  # already a clear way?
                floor,
                reached,
                idx,
                lambda i: 0 if i in opened else (None if floor.furniture[i] else walls_only(i)),  # noqa: B023
            )
            if clear is not None:
                reached.update(clear)
                continue
            free = _cheapest_path(floor, reached, idx, lambda i: 0 if i in opened else with_furniture(i))  # noqa: B023
            if free is not None:
                name = f"walkway {first}-{door}"
                for k, i in enumerate(free):
                    if k % PATH_BOX_STEP == 0 or k == len(free) - 1:
                        cx = floor.regions.x0 + (i % floor.regions.nx + 0.5) * cell
                        cy = floor.regions.y0 + (i // floor.regions.nx + 0.5) * cell
                        walkways.append((name, Box(cx, cy, 1.0, 0.0, PATH_WIDTH / 2, PATH_WIDTH / 2)))
                opened.update(free)
            elif free is None and (tight := _cheapest_path(floor, reached, idx, squeeze)) is not None:
                narrow.append(_Narrow(label, (first, door), [i for i in tight if floor.tight[i]]))
                free = tight
            reached.update(free or ())
    return walkways, narrow, floor


def make_walkable(
    building: Building, remove: set[str] | frozenset[str] = frozenset()
) -> tuple[Building, list[FurnitureFix]]:
    """`settle`, then keep a PATH_WIDTH wide way clear between the doors of every room: furniture in the way
    moves a little or is removed like any other conflict. A moved piece may block another way, so this repeats
    a few times. Passages the walls make too narrow are left to `_narrow_findings`."""
    walkways: list[tuple[str, Box]] = []
    settled, fixes = settle(building, remove)
    for _ in range(WALKWAY_ROUNDS):
        found, _, _ = _paths(settled)
        if not found:
            break
        walkways += found
        settled, fixes = settle(building, remove, walkways)
    return settled, fixes


# --- Findings ------------------------------------------------------------------------------------

MAX_FINDINGS = 60
SILENT_SHIFT = 0.03  # m: smaller moves are applied without asking the AI


@dataclass
class _Builder:
    building: Building
    findings: list[CheckFinding] = field(default_factory=list)
    new_walls: int = 0

    def valid(self, commands: list[dict]) -> bool:
        """Do the option's commands apply, in order (a door may go into a wall the option adds)?"""
        building = self.building
        try:
            for command in commands:
                building = apply_edit(building, AnyCommandAdapter.validate_python(command)).building
        except EditRejected:
            return False
        return True

    def wall_id(self) -> str:
        """A fresh id for a wall an option may add (unique across all findings)."""
        taken = {w.id for w in self.building.walls}
        while True:
            self.new_walls += 1
            if (candidate := f"wall_{len(self.building.walls) + self.new_walls}") not in taken:
                return candidate

    def add(self, kind, target_ids, detail, options: list[CheckOption], default: str) -> None:
        options = [o for o in options if self.valid(o.commands)]
        if not any(o.id == default for o in options) or len(options) < 2:
            return  # nothing to decide
        self.findings.append(
            CheckFinding(id=f"F{len(self.findings) + 1}", kind=kind, target_ids=target_ids, detail=detail,
                         options=options, default=default)
        )  # fmt: skip


def _door_option(wall: Wall, offset: float, width: float, description: str) -> CheckOption:
    command = {"operation": "add_opening", "targetId": wall.id,
               "parameters": {"type": "door", "offset": round(offset, 3), "width": width}}  # fmt: skip
    return CheckOption(id=f"door:{wall.id}@{offset:.2f}", description=description, commands=[command])


def _between(spaces: _Spaces, idx: int | None, group: set[int] | None) -> bool:
    """Is the cell in `group` (region labels), or outdoors when group is None?"""
    if idx is None:
        return False
    if group is None:
        return bool(spaces.regions.outdoor[idx])
    return not spaces.regions.outdoor[idx] and spaces.regions.labels[idx] in group


def _connections(building: Building, spaces: _Spaces, inner: set[int], outer: set[int] | None) -> list[CheckOption]:
    """Ways to connect the `inner` regions to `outer` (None: outdoors): a door in a shared wall, a
    window there turned into a door, or a short wall that may be a door leaf drawn as a wall removed."""
    regions = spaces.regions
    doors: list[tuple[float, CheckOption]] = []
    converts: list[CheckOption] = []
    plugs: list[CheckOption] = []
    step = max(regions.cell, 0.05)
    for wall in building.walls:
        length = wall.length

        def facing(s: float, wall: Wall = wall) -> int | None:
            left, right = _sides(regions, wall, s)
            if _between(spaces, left, inner) and _between(spaces, right, outer):
                return right
            if _between(spaces, right, inner) and _between(spaces, left, outer):
                return left
            return None

        for opening in wall.openings:
            if opening.type == "window" and opening.width <= MAX_CONVERTIBLE_WINDOW:
                if facing(opening.offset + opening.width / 2) is not None:
                    converts.append(
                        CheckOption(
                            id=f"convert:{opening.id}",
                            description=f"Turn window {opening.id} ({opening.width:.2f} m) on {wall.id} into a door",
                            commands=[{"operation": "set_opening_type", "targetId": opening.id,
                                       "parameters": {"type": "door"}}],
                        )
                    )  # fmt: skip
        if PLUG_WALL[0] <= length <= PLUG_WALL[1] and facing(length / 2) is not None:
            plugs.append(
                CheckOption(
                    id=f"open:{wall.id}",
                    description=f"Remove {wall.id} ({length:.2f} m): likely a door leaf or threshold drawn on the "
                    "wall layer; leaves an open doorway",
                    commands=[{"operation": "delete_wall", "targetId": wall.id, "parameters": {}}],
                )
            )
        if length < DOOR_WIDTH + 2 * END_CLEARANCE:
            continue
        taken = [(o.offset - 0.1, o.offset + o.width + 0.1) for o in wall.openings]
        run: list[float] = []
        best: tuple[float, float, int] | None = None  # (start, end, neighbor cell) of the longest run
        s = END_CLEARANCE
        while s <= length - END_CLEARANCE + 1e-9:
            neighbor = None if any(lo <= s <= hi for lo, hi in taken) else facing(s)
            if neighbor is not None:
                if not run:
                    run = [s, s, neighbor]
                run[1] = s
            if (neighbor is None or s + step > length - END_CLEARANCE) and run:
                if best is None or run[1] - run[0] > best[1] - best[0]:
                    best = (run[0], run[1], run[2])
                run = []
            s += step
        if best is None or best[1] - best[0] < DOOR_WIDTH:
            continue
        width = WIDE_DOOR_WIDTH if best[1] - best[0] >= WIDE_DOOR_WIDTH + 0.2 else DOOR_WIDTH
        offset = (best[0] + best[1]) / 2 - width / 2
        target = "outdoors" if outer is None else regions.name(regions.labels[best[2]])
        rank = (1.0 if outer is not None and CIRCULATION.search(target) else 0.0) + (best[1] - best[0]) / 100
        description = f"Add a {width:.2f} m door on {wall.id} ({length:.2f} m long) to {target}"
        doors.append((rank, _door_option(wall, offset, width, description)))
    doors.sort(key=lambda d: -d[0])
    return [*converts[:3], *plugs[:3], *(option for _, option in doors[:MAX_DOOR_CANDIDATES])]


def _space_findings(b: _Builder, spaces: _Spaces) -> None:
    building, regions = b.building, spaces.regions
    furniture_in: dict[int, list[str]] = defaultdict(list)
    for item in building.furniture:
        idx = regions.index(item.position)
        if idx is not None and regions.labels[idx] >= 0:
            furniture_in[regions.labels[idx]].append(item.category)

    keep_closed = CheckOption(id="keep", description="Keep it closed (a shaft, duct, light well or storage void)")
    main_labels = _component_labels(spaces, spaces.main)
    if spaces.main != spaces.outside:
        area = sum(regions.areas[label] for label in main_labels)
        rooms = sorted({regions.name(label) for label in main_labels if label in regions.names})
        b.add(
            "no_exterior_door", [], f"No door leads from the building ({area:.0f} m²; rooms: "
            f"{', '.join(rooms[:8]) or 'unnamed'}) to the outside.",
            [*_connections(building, spaces, main_labels, None),
             CheckOption(id="keep", description="Keep as drawn (the entrance is outside this drawing)")],
            "keep",
        )  # fmt: skip
    roots = {root for root in spaces.space_of.values() if root not in (spaces.main, spaces.outside)}
    for root in sorted(roots):
        labels = _component_labels(spaces, root)
        area = sum(regions.areas[label] for label in labels)
        if not any(regions.is_space(label) for label in labels):
            continue
        contents = [c for label in labels for c in furniture_in.get(label, [])]
        names = sorted({regions.name(label) for label in labels})
        detail = f"{' + '.join(names)} ({area:.1f} m²) has no door from the rest of the building" + (
            f"; it contains {', '.join(sorted(contents))}" if contents else "; it is empty"
        )
        # Connect to the reachable part of the building, else straight outdoors.
        options = _connections(building, spaces, labels, main_labels) or _connections(building, spaces, labels, None)
        b.add("enclosed_space", [], detail, [*options, keep_closed], "keep")


def _indoor(regions: _Regions, idx: int | None) -> bool:
    if idx is None or regions.outdoor[idx]:
        return False
    label = regions.labels[idx]
    return regions.is_space(label) or (label == regions.exterior and regions.leaked_area >= LEAKED_BUILDING)


def _window_findings(b: _Builder, spaces: _Spaces) -> None:
    regions = spaces.regions
    for wall in b.building.walls:
        for window in (o for o in wall.openings if o.type == "window"):
            left, right = _sides(regions, wall, window.offset + window.width / 2)
            if not (_indoor(regions, left) and _indoor(regions, right)):
                continue
            between = f"{regions.name(regions.labels[left])} and {regions.name(regions.labels[right])}"
            options = []
            if window.width <= MAX_CONVERTIBLE_WINDOW:
                options.append(
                    CheckOption(id="convert", description=f"Turn it into a door between {between}",
                                commands=[{"operation": "set_opening_type", "targetId": window.id,
                                           "parameters": {"type": "door"}}])
                )  # fmt: skip
            options += [
                CheckOption(id="remove", description="Remove it (plain wall)",
                            commands=[{"operation": "delete_opening", "targetId": window.id, "parameters": {}}]),
                CheckOption(id="keep", description="Keep it: an interior glazed partition or borrowed light"),
            ]  # fmt: skip
            b.add(
                "interior_window", [window.id, wall.id],
                f"Window {window.id} ({window.width:.2f} m) on {wall.id} is between two indoor spaces: {between}.",
                options, "keep",
            )  # fmt: skip


FACADE_GAP = (0.6, 4.0)  # m between a free wall end and the wall its line runs into
ENTRANCE_DOOR = 0.9
SIDE_PROBE = 0.6  # m either side of a gap: one side outside the building's outline, one inside


def _connected(point: Point2, wall: Wall, walls: list[Wall]) -> bool:
    return any(
        other is not wall
        and point_segment_distance(point, other.start, other.end) <= other.thickness / 2 + MAX_JOINT_GAP
        for other in walls
    )


@dataclass
class _Gap:
    wall: Wall  # the wall that stops in mid-air
    point: Point2  # its free end
    outward: Point2
    q: Point2  # where its line reaches the next wall
    other: Wall
    ahead: float  # free end -> q
    gap: float  # clear width of the hole


def _wall_gaps(walls: list[Wall], widest: float) -> list[_Gap]:
    """Holes between a wall's free end and the next wall along its line: a wall running into another one,
    or a collinear piece of the same wall further on. Each hole once."""
    gaps: list[_Gap] = []
    seen: set[tuple] = set()
    for wall in walls:
        if wall.length <= 0:
            continue
        d = unit(sub(wall.end, wall.start))
        for point, outward in ((wall.end, d), (wall.start, scale(d, -1.0))):
            if _connected(point, wall, walls):
                continue
            hit: tuple[float, Wall, Point2, float] | None = None  # (ahead, other, q, clear gap)
            for other in walls:
                if other is wall or other.length <= 0:
                    continue
                od = unit(sub(other.end, other.start))
                if abs(outward[0] * od[1] - outward[1] * od[0]) < 0.05:  # parallel: a collinear piece?
                    offset = abs(outward[0] * (other.start[1] - point[1]) - outward[1] * (other.start[0] - point[0]))
                    if offset > (wall.thickness + other.thickness) / 2:
                        continue
                    for end in (other.start, other.end):
                        ahead = dot(sub(end, point), outward)
                        if 0.05 < ahead and (hit is None or ahead < hit[0]):
                            hit = (ahead, other, add(point, scale(outward, ahead)), ahead)
                    continue
                q = line_intersection(point, add(point, outward), other.start, other.end)
                if q is None:
                    continue
                along = project_param(q, other.start, other.end)
                ahead = dot(sub(q, point), outward)
                if -other.thickness <= along <= other.length + other.thickness and 0.05 < ahead:
                    if hit is None or ahead < hit[0]:
                        hit = (ahead, other, q, ahead - other.thickness / 2)
            if hit is None or not FACADE_GAP[0] <= hit[3] <= widest:
                continue
            ahead, other, q, gap = hit
            key = tuple(sorted((tuple(round(v, 1) for v in point), tuple(round(v, 1) for v in q))))
            if key not in seen:
                seen.add(key)
                gaps.append(_Gap(wall, point, outward, q, other, ahead, gap))
    return gaps


def _gap_findings(b: _Builder) -> None:
    """Holes in the facade: a wall that stops in mid-air while its line runs into another wall a little
    further on, with the building's outside on one side of the hole. Usually a wall piece (or the entrance
    door, or glazing) the drawing or the parser lost; sometimes a real open porch or terrace."""
    walls = b.building.walls
    hull = _hull([corner for _, box in wall_boxes(walls) for corner in _corners(box)])
    for hole in _wall_gaps(walls, FACADE_GAP[1]):
        wall, point, outward, q, other, ahead, gap = (
            hole.wall,
            hole.point,
            hole.outward,
            hole.q,
            hole.other,
            hole.ahead,
            hole.gap,
        )
        middle = add(point, scale(outward, gap / 2))
        n = (-outward[1], outward[0])
        sides = [_outside_hull(add(middle, scale(n, s * SIDE_PROBE)), hull) > 0.05 for s in (1, -1)]
        if sides[0] == sides[1]:
            continue  # inside the building: a passage between rooms
        new_id = b.wall_id()
        add_wall = {"operation": "add_wall", "targetId": new_id,
                    "parameters": {"start": list(point), "end": list(q), "thickness": wall.thickness}}  # fmt: skip
        length = ahead
        options = []
        door = min(ENTRANCE_DOOR, gap - 0.2)
        if door >= 0.6:
            offset = (length - door) / 2
            options.append(CheckOption(
                id="entrance", description=f"Close it with a wall holding a {door:.2f} m entrance door",
                commands=[add_wall, {"operation": "add_opening", "targetId": new_id,
                                     "parameters": {"type": "door", "offset": round(offset, 3), "width": door}}],
            ))  # fmt: skip
        if gap >= 1.5:
            glass = round(gap - 0.6, 3)
            options.append(CheckOption(
                id="glazing", description=f"Close it with a wall holding a {glass:.2f} m window (glazed facade)",
                commands=[add_wall, {"operation": "add_opening", "targetId": new_id,
                                     "parameters": {"type": "window", "offset": round((length - glass) / 2, 3),
                                                    "width": glass}}],
            ))  # fmt: skip
        options += [
            CheckOption(id="wall", description="Close it with a plain wall", commands=[add_wall]),
            CheckOption(id="keep", description="Keep it open (a real open porch, terrace or passage)"),
        ]
        b.add(
            "facade_gap", [wall.id, other.id],
            f"The outer wall has a {gap:.2f} m hole with no window or door between {wall.id} (it stops in "
            f"mid-air) and {other.id}.",
            options, "entrance" if door >= 0.6 else "wall",
        )  # fmt: skip


BATHROOM = re.compile(
    r"bath|ba[ñn]o|aseo|\bwc\b|w\.c|toilet|lavabo|restroom|lavatory|shower|ducha|servicio|sanitari", re.IGNORECASE
)
BATHROOM_GAP = 2.0  # m: widest hole considered a bathroom's door-free connection


def _region_label(regions: _Regions, point: Point2) -> int | None:
    idx = regions.index(point)
    return None if idx is None or regions.labels[idx] < 0 else regions.labels[idx]


BATH_FIXTURES = ("toilet", "shower", "bathtub")


def _bathroom_findings(b: _Builder) -> None:
    """A bathroom either has doors, or it has none; a bathroom with a door that is also open to another room
    (a wall piece the drawing or parser lost, e.g. an angled wall around a shower) is impossible. Bathrooms
    are found by their fixtures (toilet, shower, bathtub), since room labels are often printed beside the
    room. A bathroom shares its space with another room when that room's label is in the same free area;
    then the hole whose closing separates them is the finding."""
    building = b.building
    fixtures = [f for f in building.furniture if f.category in BATH_FIXTURES]
    if not fixtures:
        return
    labelled = [(room.name, room.label_position) for room in building.rooms if room.label_position]
    regions = _regions(building, [box for _, box in wall_boxes(building.walls)])
    if regions is None:
        return
    gaps = _wall_gaps(building.walls, BATHROOM_GAP)
    done: set[int] = set()
    for fixture in fixtures:
        label = _region_label(regions, fixture.position)
        if label is None or label in done or regions.outdoor[regions.index(fixture.position)]:
            continue
        done.add(label)
        here = [(n, pos) for n, pos in labelled if _region_label(regions, pos) == label]
        others = [n for n, _ in here if not BATHROOM.search(n)]
        if not others:
            continue  # a room of its own
        name = next((n for n, _ in here if BATHROOM.search(n)), f"The bathroom with {fixture.category} {fixture.id}")
        for hole in sorted(gaps, key=lambda g: math.dist(g.point, fixture.position)):
            wall_id = b.wall_id()
            sealed = Wall(id=wall_id, start=hole.point, end=hole.q, thickness=hole.wall.thickness,
                          height=building.ceiling_height)  # fmt: skip
            test = building.model_copy(update={"walls": [*building.walls, sealed]})
            split = _regions(test, [box for _, box in wall_boxes(test.walls)])
            mine = _region_label(split, fixture.position) if split else None
            if mine is None or any(_region_label(split, pos) == mine for n, pos in here if not BATHROOM.search(n)):
                b.new_walls -= 1  # closing this hole does not separate the bathroom: give the id back
                continue
            has_door = any(
                any(i is not None and split.labels[i] == mine for i in _sides(split, w, o.offset + o.width / 2))
                for w in building.walls
                for o in w.openings
                if o.type == "door"
            )
            seal = {"start": list(hole.point), "end": list(hole.q), "thickness": hole.wall.thickness}
            add_wall = {"operation": "add_wall", "targetId": wall_id, "parameters": seal}
            options = [CheckOption(id="wall", description=f"Close the {hole.gap:.2f} m opening with a wall",
                                   commands=[add_wall])]  # fmt: skip
            door = min(0.8, hole.gap - 0.1)
            if door >= 0.6:
                offset = round((hole.ahead - door) / 2, 3)
                options.append(CheckOption(
                    id="door", description=f"Put a {door:.2f} m door in the opening",
                    commands=[add_wall, {"operation": "add_opening", "targetId": wall_id,
                                         "parameters": {"type": "door", "offset": offset, "width": door}}],
                ))  # fmt: skip
            if not has_door:
                options.append(CheckOption(id="keep", description="Keep it open: a bathroom without any door"))
            b.add(
                "bathroom_open", [hole.wall.id, hole.other.id],
                f"{name} {'has a door but ' if has_door else ''}is also open to {', '.join(sorted(set(others))[:4])} "
                f"through a {hole.gap:.2f} m opening between {hole.wall.id} and {hole.other.id}.",
                options, "wall" if has_door else "door",
            )  # fmt: skip
            break


OPEN_SIDE = (1.0, 12.0)  # m between two free wall ends that may be the two sides of a missing outer wall
OUTDOOR_ROOM = re.compile(
    r"balc|terra|veranda|porch|patio|yard|garden|jard[ií]n|deck|loggia|galer[ií]a|porche|tendedero|azotea",
    re.IGNORECASE,
)
WIDE_WINDOW = 1.3  # x the building's typical window: a missing wall most likely held a wider window


def _free_ends(walls: list[Wall]) -> list[tuple[Wall, Point2]]:
    out = []
    for wall in walls:
        if wall.length > 0:
            out += [(wall, p) for p in (wall.start, wall.end) if not _connected(p, wall, walls)]
    return out


def _behind(p: Point2, middle: Point2, inside: Point2, reach: float) -> bool:
    """Is `p` on the building's side of an opening centered at `middle`, within `reach`?"""
    return math.dist(p, middle) <= reach and dot(sub(p, middle), inside) > 0


def _missing_wall_findings(b: _Builder) -> None:
    """A room missing an entire outer wall: two walls stop in mid-air facing each other across a side of the
    building, so the room (and its furniture) opens straight to the street. Impossible in a house unless it is
    a balcony, terrace, veranda or yard; the lost wall most likely held a window a bit wider than the others."""
    building = b.building
    walls = building.walls
    hull = _hull([corner for _, box in wall_boxes(walls) for corner in _corners(box)])
    taken = {tuple(round(v, 3) for v in g.point) for g in _wall_gaps(walls, FACADE_GAP[1])}  # facade holes
    ends = [(w, p) for w, p in _free_ends(walls) if tuple(round(v, 3) for v in p) not in taken]
    windows = sorted(o.width for w in walls for o in w.openings if o.type == "window")
    typical = windows[len(windows) // 2] if windows else 1.2
    pairs = []
    for i, (wall_a, a) in enumerate(ends):
        for wall_b, c in ends[i + 1 :]:
            length = math.dist(a, c)
            if wall_a is wall_b or not OPEN_SIDE[0] <= length <= OPEN_SIDE[1]:
                continue
            d = unit(sub(c, a))
            inner = (add(a, scale(d, 0.1)), sub(c, scale(d, 0.1)))
            if any(segments_intersect(*inner, w.start, w.end) for w in walls if w is not wall_a and w is not wall_b):
                continue
            middle = scale(add(a, c), 0.5)
            n = (-d[1], d[0])
            sides = [_outside_hull(add(middle, scale(n, s * SIDE_PROBE)), hull) > 0.05 for s in (1, -1)]
            if sides[0] == sides[1]:
                continue  # not on the building's outline
            inside = scale(n, -1.0 if sides[0] else 1.0)
            pairs.append((length, wall_a, a, wall_b, c, inside))
    used: set[tuple] = set()
    for length, wall_a, a, wall_b, c, inside in sorted(pairs, key=lambda p: p[0]):
        if tuple(a) in used or tuple(c) in used:
            continue
        used |= {tuple(a), tuple(c)}
        middle = scale(add(a, c), 0.5)
        reach = length / 2 + 4.0
        names = sorted(
            {r.name for r in building.rooms if r.label_position and _behind(r.label_position, middle, inside, reach)}
        )
        items = [f for f in building.furniture if _behind(f.position, middle, inside, reach)]
        wall_id = b.wall_id()
        add_wall = {"operation": "add_wall", "targetId": wall_id,
                    "parameters": {"start": list(a), "end": list(c),
                                   "thickness": max(wall_a.thickness, wall_b.thickness)}}  # fmt: skip
        window = round(min(length - 0.6, max(1.0, typical * WIDE_WINDOW)), 3)
        door = round(min(0.9, length - 0.3), 3)
        options = []
        if window >= 0.6:
            options.append(CheckOption(
                id="window", description=f"Close it with a wall holding a {window:.2f} m window (the lost one)",
                commands=[add_wall, {"operation": "add_opening", "targetId": wall_id,
                                     "parameters": {"type": "window", "offset": round((length - window) / 2, 3),
                                                    "width": window}}],
            ))  # fmt: skip
        if door >= 0.7:
            options.append(CheckOption(
                id="door", description=f"Close it with a wall holding a {door:.2f} m door (entrance or terrace)",
                commands=[add_wall, {"operation": "add_opening", "targetId": wall_id,
                                     "parameters": {"type": "door", "offset": round((length - door) / 2, 3),
                                                    "width": door}}],
            ))  # fmt: skip
        options += [
            CheckOption(id="wall", description="Close it with a plain wall", commands=[add_wall]),
            CheckOption(id="keep", description="Keep it open: a balcony, terrace, veranda, porch or yard"),
        ]
        outdoor = any(OUTDOOR_ROOM.search(n) for n in names)
        what = f" with {_what(items)}" if items else ""
        b.add(
            "missing_wall", [wall_a.id, wall_b.id],
            f"{' / '.join(names) or 'A room'}{what} is open to the outside along {length:.2f} m between "
            f"{wall_a.id} and {wall_b.id}: no wall, window or door there.",
            options, "keep" if outdoor else "window" if window >= 0.6 else "wall",
        )  # fmt: skip


# Furniture from contradictory contexts never shares a room: a toilet (shower, bathtub) with a kitchen unit
# or stove, a kitchen sink, a fridge, or living/sleeping furniture. Washbasins are not modelled, so a
# bathroom's own sink never counts; a kitchen with a bed (a studio) or a sofa (open plan) is fine.
NEVER_WITH_BATH_FIXTURES = ("kitchen", "sink", "fridge", "bed", "sofa", "desk", "table", "bookshelf", "nightstand",
                            "dresser")  # fmt: skip
CATEGORY_WORDS = {"sink": "kitchen sink", "kitchen": "kitchen unit"}


def _with_defaults(building: Building, findings: list[CheckFinding], kinds: tuple[str, ...]) -> Building:
    """The building with the default option of some findings applied (to judge rooms as they will be)."""
    for finding in findings:
        if finding.kind not in kinds:
            continue
        option = next(o for o in finding.options if o.id == finding.default)
        for raw in option.commands:
            try:
                building = apply_edit(building, AnyCommandAdapter.validate_python(raw)).building
            except EditRejected:
                break
    return building


def _what(items: list[FurnitureItem]) -> str:
    return ", ".join(f"{CATEGORY_WORDS.get(i.category, i.category)} {i.id}" for i in items[:4]) + (
        f" (+{len(items) - 4})" if len(items) > 4 else ""
    )


@dataclass
class _RoomContents:
    """Indoor free areas of a building (region label ->) with the furniture and room labels inside them."""

    items: dict[int, list[FurnitureItem]]
    names: dict[int, list[str]]


def _room_contents(building: Building, rooms_from: Building) -> _RoomContents | None:
    """`building`'s furniture and `rooms_from`'s labels, grouped by the free areas of `rooms_from`."""
    regions = _regions(rooms_from, [box for _, box in wall_boxes(rooms_from.walls)])
    if regions is None:
        return None
    items: dict[int, list[FurnitureItem]] = defaultdict(list)
    for item in building.furniture:
        idx = regions.index(item.position)
        if idx is not None and regions.labels[idx] >= 0 and not regions.outdoor[idx]:
            items[regions.labels[idx]].append(item)
    names: dict[int, list[str]] = defaultdict(list)
    for room in rooms_from.rooms:
        if room.label_position and (label := _region_label(regions, room.label_position)) is not None:
            names[label].append(room.name)
    return _RoomContents(items, names)


def _incompatible_findings(b: _Builder, rooms: _RoomContents) -> None:
    """Rooms holding bathroom fixtures together with things that never stand in a bathroom: one of the two
    groups is misidentified (a washbasin read as a kitchen sink, a symbol read as a toilet), or a wall between
    two rooms is missing."""
    names = rooms.names
    for label, items in sorted(rooms.items.items()):
        bath = [i for i in items if i.category in BATH_FIXTURES]
        other = [i for i in items if i.category in NEVER_WITH_BATH_FIXTURES]
        if not bath or not other:
            continue
        here = names.get(label, [])
        bath_named = any(BATHROOM.search(n) for n in here)
        other_named = any(not BATHROOM.search(n) for n in here)
        if bath_named != other_named:
            default = "remove_other" if bath_named else "remove_bath"
        else:
            default = "remove_other" if len(other) <= len(bath) else "remove_bath"
        b.add(
            "incompatible_furniture", [i.id for i in bath + other],
            f"{_what(bath)} and {_what(other)} stand in the same room ({' / '.join(here) or 'unnamed'}): bathroom "
            "fixtures never share a room with these.",
            [CheckOption(id="remove_other", description=f"Remove {_what(other)} (misidentified: this is a bathroom)",
                         remove_furniture=[i.id for i in other]),
             CheckOption(id="remove_bath", description=f"Remove {_what(bath)} (misidentified: this is not a bathroom)",
                         remove_furniture=[i.id for i in bath]),
             CheckOption(id="keep", description="Keep both: they are in two rooms and a wall between them is missing")],
            default,
        )  # fmt: skip


# A labelled room's type, from its label (English and Spanish drawings). A room whose labels name several types
# (open plan: "Cocina-comedor", "Kitchen / Living") takes them all; unlabelled rooms, halls, storage, terraces
# and the like are not judged (the bathroom-fixture rule above still applies to them).
ROOM_TYPES = {
    "bathroom": re.compile(BATHROOM.pattern + r"|en.?suite", re.IGNORECASE),
    "kitchen": re.compile(r"kitchen|cocina|pantry|despensa", re.IGNORECASE),
    "bedroom": re.compile(r"bed|dormitor|habitaci[oó]n|alcoba|rec[aá]mara|nursery|guest", re.IGNORECASE),
    "living": re.compile(r"living|lounge|family|sitting|dining|sal[oó]n|estar|comedor", re.IGNORECASE),
    "office": re.compile(r"office|study|despacho|oficina|meeting|conference|reuni[oó]n", re.IGNORECASE),
    "studio": re.compile(r"studio|estudio|loft|monoambiente", re.IGNORECASE),
}
# What never stands in each type of room. Open-plan kitchens (in a living room, an office kitchenette, a Spanish
# "office" by the kitchen) and studios are fine. A category is out of place only when every type of its room
# forbids it.
_BED_ROOM = ("bed", "nightstand", "dresser")
_KITCHEN = ("kitchen", "sink", "fridge")
NEVER_IN = {
    "bathroom": NEVER_WITH_BATH_FIXTURES,
    "kitchen": (*BATH_FIXTURES, *_BED_ROOM),
    "bedroom": (*BATH_FIXTURES, *_KITCHEN),
    "living": (*BATH_FIXTURES, "bed"),
    "office": (*BATH_FIXTURES, "bed"),
    "studio": BATH_FIXTURES,
}
# What makes a room recognisably the type its label says: when none of it is there, the label more likely
# belongs to a neighbouring room (labels are often printed beside their room) than the furniture is wrong.
TYPICAL = {
    "bathroom": BATH_FIXTURES,
    "kitchen": _KITCHEN,
    "bedroom": _BED_ROOM,
    "living": ("sofa", "table"),
    "office": ("desk", "bookshelf"),
    "studio": ("bed", *_KITCHEN, "sofa"),
}
ROOM_WORDS = {"living": "living or dining room", "office": "office or study"}


def _misplaced_findings(b: _Builder, rooms: _RoomContents, flagged: set[str]) -> None:
    """Furniture that does not belong in the type of room it stands in (a toilet in the kitchen, a bed in the
    living room, a fridge in a bedroom): misidentified, or the room's label is misplaced. Items in `flagged`
    already have a finding of their own."""
    for label, items in sorted(rooms.items.items()):
        names = rooms.names.get(label, [])
        types = [kind for kind, pattern in ROOM_TYPES.items() if any(pattern.search(n) for n in names)]
        if not types:
            continue
        misplaced = [
            i for i in items if i.id not in flagged and all(i.category in NEVER_IN[kind] for kind in types)
        ]  # fmt: skip
        if not misplaced:
            continue
        what = " / ".join(ROOM_WORDS.get(kind, kind) for kind in types)
        typical = any(i.category in TYPICAL[kind] for i in items if i not in misplaced for kind in types)
        b.add(
            "misplaced_furniture", [i.id for i in misplaced],
            f"{_what(misplaced)} stand{'s' if len(misplaced) == 1 else ''} in {' / '.join(names)} (a {what}), "
            f"where {'it does' if len(misplaced) == 1 else 'they do'} not belong"
            + ("." if typical else f"; nothing else there is typical of a {what}."),
            [CheckOption(id="remove", description=f"Remove {_what(misplaced)} (misidentified, or not part of this "
                                                  "room)", remove_furniture=[i.id for i in misplaced]),
             CheckOption(id="keep", description="Keep it: the room's label is misplaced (it names a neighbouring "
                                                "room) or wrong")],
            "remove" if typical else "keep",
        )  # fmt: skip


def _cell_center(regions: _Regions, idx: int) -> Point2:
    return (regions.x0 + (idx % regions.nx + 0.5) * regions.cell, regions.y0 + (idx // regions.nx + 0.5) * regions.cell)


def _narrow_findings(b: _Builder, narrow: list[_Narrow], floor: _Floor) -> None:
    """Passages between two parallel walls narrower than PATH_WIDTH on the only way between two doors (a
    hallway, the gap beside a stair or a built-in): one of the walls moves out by the difference, into the
    bigger room behind it by default (outdoors last: that changes the facade)."""
    regions = floor.regions
    walls = [w for w in b.building.walls if w.length > 0]
    seen: set[frozenset[str]] = set()
    for passage in narrow:
        best: tuple[float, Wall, Wall, Point2] | None = None
        for idx in passage.cells[::2]:
            p = _cell_center(regions, idx)
            reach = PATH_WIDTH / 2 + regions.cell
            near = [w for w in walls if point_segment_distance(p, w.start, w.end) <= w.thickness / 2 + reach]
            # both walls run alongside p (not one of them ending just before it)
            near = [w for w in near if 0.0 <= project_param(p, w.start, w.end) <= w.length]
            for w1, w2 in combinations(near, 2):
                if abs(cross(unit(sub(w1.end, w1.start)), unit(sub(w2.end, w2.start)))) > 0.05:
                    continue
                n1 = left_normal(w1.start, w1.end)
                at, other = dot(sub(p, w1.start), n1), dot(sub(w2.start, w1.start), n1)
                if at * (other - at) <= 0:
                    continue  # p is not between the two
                gap = abs(other) - (w1.thickness + w2.thickness) / 2
                if gap < PATH_WIDTH and (best is None or gap < best[0]):
                    best = (gap, w1, w2, p)
        if best is None:
            continue
        gap, w1, w2, p = best
        widen = round(PATH_WIDTH - gap + 0.02, 3)
        if frozenset((w1.id, w2.id)) in seen or widen > MAX_WIDEN:
            continue
        seen.add(frozenset((w1.id, w2.id)))
        ranked: list[tuple[float, CheckOption]] = []
        for w in (w1, w2):
            n = left_normal(w.start, w.end)
            away = -1.0 if dot(sub(p, w.start), n) > 0 else 1.0
            along = add(w.start, scale(unit(sub(w.end, w.start)), project_param(p, w.start, w.end)))
            far = regions.probe(along, scale(n, away), w.thickness / 2)
            if far is None or regions.outdoor[far]:
                rank, into = -1.0, "outdoors"
            else:
                rank, into = regions.areas.get(regions.labels[far], 0.0), regions.name(regions.labels[far])
            option = CheckOption(
                id=f"widen:{w.id}", description=f"Move {w.id} {widen:.2f} m out, into {into}",
                commands=[{"operation": "shift_wall", "targetId": w.id, "parameters": {"offset": away * widen}}],
            )  # fmt: skip
            if b.valid(option.commands):
                ranked.append((rank, option))
        if not ranked:
            continue
        ranked.sort(key=lambda r: -r[0])
        b.add(
            "narrow_passage", [w1.id, w2.id],
            f"The way between {passage.doors[0]} and {passage.doors[1]} through {regions.name(passage.label)} is only "
            f"{gap:.2f} m wide between {w1.id} and {w2.id}; walking needs {PATH_WIDTH:.2f} m.",
            [*(option for _, option in ranked),
             CheckOption(id="keep", description="Keep it as drawn (a real tight spot, or the drawing is right)")],
            ranked[0][1].id,
        )  # fmt: skip


def _hull(points: list[Point2]) -> list[Point2]:
    """Convex hull, counter-clockwise (Andrew's monotone chain)."""
    pts = sorted(set(points))
    if len(pts) < 3:
        return pts

    def turn(o: Point2, a: Point2, c: Point2) -> float:
        return (a[0] - o[0]) * (c[1] - o[1]) - (a[1] - o[1]) * (c[0] - o[0])

    lower: list[Point2] = []
    upper: list[Point2] = []
    for p in pts:
        while len(lower) >= 2 and turn(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and turn(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def _outside_hull(point: Point2, hull: list[Point2]) -> float:
    """How far `point` lies outside the convex polygon (0 when inside or on it)."""
    if len(hull) < 3:
        return 0.0
    n = len(hull)
    inside = all(
        (hull[(i + 1) % n][0] - hull[i][0]) * (point[1] - hull[i][1])
        - (hull[(i + 1) % n][1] - hull[i][1]) * (point[0] - hull[i][0])
        >= 0
        for i in range(n)
    )
    if inside:
        return 0.0
    return min(point_segment_distance(point, hull[i], hull[(i + 1) % n]) for i in range(n))


def _outside_findings(b: _Builder) -> None:
    """Furniture beyond the building's footprint (the convex outline of its walls): drawing artefacts
    such as legends or symbol libraries, or outdoor furniture on a terrace."""
    walls = b.building.walls
    hull = _hull([corner for _, box in wall_boxes(walls) for corner in _corners(box)])
    for item in b.building.furniture:
        if _outside_hull(item.position, hull) <= OUTSIDE_HULL_MARGIN:
            continue
        gap = min((point_segment_distance(item.position, w.start, w.end) - w.thickness / 2 for w in walls), default=0)
        b.add(
            "furniture_outside", [item.id],
            f"{item.category} {item.id} ({item.width:.2f} x {item.depth:.2f} m, block {item.source_block!r}) stands "
            f"outside the building, {gap:.1f} m from the nearest wall.",
            [CheckOption(id="remove", description="Remove it (a drawing artefact: legend, symbol library, other sheet)",
                         remove_furniture=[item.id]),
             CheckOption(id="keep", description="Keep it (furniture on a balcony, terrace or garden)")],
            "remove" if gap > OUTSIDE_FURNITURE_GAP else "keep",
        )  # fmt: skip


def _corners(box: Box) -> list[Point2]:
    vx, vy = -box.uy, box.ux
    return [
        (box.cx + su * box.hu * box.ux + sv * box.hv * vx, box.cy + su * box.hu * box.uy + sv * box.hv * vy)
        for su in (-1, 1)
        for sv in (-1, 1)
    ]


def _furniture_findings(b: _Builder, fixes: list[FurnitureFix]) -> None:
    items = {f.id: f for f in b.building.furniture}
    for fix in fixes:
        if fix.action != "removed" and fix.shift < SILENT_SHIFT and fix.scale >= 1:
            continue
        others = [c for c in fix.conflicts if c in items]
        options = [CheckOption(id="accept", description=fix.summary)]
        if fix.action != "removed":
            options.append(
                CheckOption(id="remove", description=f"Remove {fix.category} {fix.item_id} instead",
                            remove_furniture=[fix.item_id])
            )  # fmt: skip
        for other in others[:2]:
            options.append(
                CheckOption(id=f"remove_other:{other}",
                            description=f"Remove {items[other].category} {other} instead and keep {fix.item_id}",
                            remove_furniture=[other])
            )  # fmt: skip
        item = items[fix.item_id]
        drawn = item.drawn or item
        b.add(
            "furniture_conflict", [fix.item_id, *others],
            f"{fix.category} {fix.item_id} (block {item.source_block!r}, drawn {drawn.width:.2f} x {drawn.depth:.2f} m"
            f" at {drawn.position}) cuts into {', '.join(fix.conflicts[:3])}.",
            options, "accept",
        )  # fmt: skip


@dataclass
class Analysis:
    findings: list[CheckFinding]
    notes: list[str]


def analyze(building: Building) -> Analysis:
    """Every judgement call the scene needs, each with precomputed options and a default."""
    b = _Builder(building)
    notes: list[str] = []
    _gap_findings(b)
    _missing_wall_findings(b)
    _bathroom_findings(b)
    rooms = _room_contents(
        building, _with_defaults(building, b.findings, ("facade_gap", "missing_wall", "bathroom_open"))
    )
    if rooms is not None:
        _incompatible_findings(b, rooms)
        flagged = {i for f in b.findings if f.kind == "incompatible_furniture" for i in f.target_ids}
        _misplaced_findings(b, rooms, flagged)
    regions = _regions(building, [box for _, box in wall_boxes(building.walls)])
    if regions is None:
        if building.walls:
            notes.append("The drawing is too large for the room checks (enclosed spaces, interior windows).")
    else:
        spaces = _space_graph(building, regions)
        _space_findings(b, spaces)
        _window_findings(b, spaces)
        _, narrow, floor = _paths(building)
        if floor is not None:
            _narrow_findings(b, narrow, floor)
    _outside_findings(b)
    _, fixes = make_walkable(building)
    _furniture_findings(b, sorted(fixes, key=lambda f: (f.action != "removed", -f.shift)))
    if len(b.findings) > MAX_FINDINGS:
        notes.append(f"{len(b.findings) - MAX_FINDINGS} more findings used their defaults (too many to review).")
    return Analysis(findings=b.findings, notes=notes)


# --- Applying decisions -------------------------------------------------------------------------


@dataclass
class Resolution:
    building: Building
    findings: list[CheckFinding]
    changes: list[CheckChange]
    notes: list[str]


def resolve(
    building: Building,
    findings: list[CheckFinding],
    patches: list[AnyCommand],
    decisions: dict[str, tuple[str, str]],
) -> Resolution:
    """Apply review patches, then each finding's chosen option (the AI's, else the default), then
    enforce the furniture rule. Edits that no longer apply (e.g. their wall was merged away) are skipped."""
    changes: list[CheckChange] = []
    notes: list[str] = []

    def apply(command: AnyCommand, source: str, label: str) -> None:
        nonlocal building
        try:
            result = apply_edit(building, command)
        except EditRejected as exc:
            if any(problem.code != "no_change" for problem in exc.problems):  # "already a door": done twice
                notes.append(f"{label} skipped: {exc}")
            return
        building = result.building
        changes.append(CheckChange(target_ids=result.affected_ids, summary=result.summary, source=source))

    for command in patches:
        apply(command, "ai", "AI fix")
    remove: set[str] = set()
    decided: list[CheckFinding] = []
    for finding in findings:
        choice, reason = decisions.get(finding.id, (None, None))
        option = next((o for o in finding.options if o.id == choice), None)
        by = "ai" if option is not None else "default"
        option = option or next(o for o in finding.options if o.id == finding.default)
        decided.append(finding.model_copy(update={"decision": option.id, "decided_by": by, "reason": reason}))
        remove.update(option.remove_furniture)
        for raw in option.commands:
            apply(
                AnyCommandAdapter.validate_python(raw),
                "ai" if by == "ai" else "guardrails",
                f"{finding.id} {option.id}",
            )
    building, fixes = make_walkable(building, remove)
    for fix in fixes:
        changes.append(
            CheckChange(target_ids=[fix.item_id], summary=fix.summary, source="ai" if fix.decided else "guardrails")
        )
    return Resolution(building=building, findings=decided, changes=changes, notes=notes)


def enforce(building: Building) -> tuple[Building, list[FurnitureFix]]:
    """The hard rules only (after manual edits): returns the building unchanged when nothing conflicts and
    every room's doors are connected by a clear path."""
    if not find_conflicts(building) and not _paths(building)[0]:
        return building, []
    return make_walkable(building)
