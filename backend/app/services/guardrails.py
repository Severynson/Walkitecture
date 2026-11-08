"""Guardrails: what the 3D scene may never show, checked and fixed before it is built (ARCHITECTURE.md §4.4).

Hard rule, always enforced by code (`settle`):
* Furniture never intersects a wall (doorways included) or another piece of furniture. A conflicting
  item moves (up to MAX_SHIFT) and/or shrinks a little (down to MIN_SCALE) to the nearest clear spot,
  or is removed when there is no room for it. Only a chair tucked under a table or desk may overlap
  it (up to CHAIR_TUCK). Bigger, more important pieces are placed first and keep their spot.

Judgement calls (`analyze`): found by code, decided by the AI among precomputed options (each one a
validated edit, so the AI can choose but never invent geometry), with a safe default without AI:
* which piece gives way in a furniture conflict;
* furniture standing outside the building;
* windows between two indoor spaces (almost always a door drawn as a window, or a misread wall);
* spaces that no door leads into, and buildings without an exterior door.

`resolve` applies the AI's review patches and decisions, then runs `settle` again, so the hard rule
holds whatever was decided. All of this is pure CPU work on a Building (run in a worker process).
"""

import math
import re
from array import array
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import product

from app.schemas.building import Building, Footprint, FurnitureItem, Point2, Wall
from app.schemas.checks import CheckChange, CheckFinding, CheckOption
from app.schemas.edits import AnyCommand, AnyCommandAdapter
from app.services.babylon_scene import MAX_JOINT_GAP, SIZE_RANGE, rendered_footprint, wall_spans
from app.services.edits import EditRejected, apply_edit
from app.services.furniture_catalog import CATALOG
from app.services.geometry import (
    SegmentGrid,
    add,
    dot,
    line_intersection,
    point_segment_distance,
    project_param,
    scale,
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


class _WallIndex:
    def __init__(self, walls: list[Wall]):
        self.items = wall_boxes(walls)
        segments = []
        for _, box in self.items:
            dx, dy = box.ux * box.hu, box.uy * box.hu
            segments.append(((box.cx - dx, box.cy - dy), (box.cx + dx, box.cy + dy)))
        self.max_half = max((box.hv for _, box in self.items), default=0.0)
        self.grid = SegmentGrid(segments, cell=2.0)

    def near(self, bbox: tuple[float, float, float, float], margin: float) -> list[tuple[str, Box]]:
        return [(self.items[i][0].id, self.items[i][1]) for i in self.grid.near(*bbox, margin=margin + self.max_half)]


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


def settle(building: Building, remove: set[str] | frozenset[str] = frozenset()) -> tuple[Building, list[FurnitureFix]]:
    """Enforce the furniture rule: returns the corrected building (input untouched) and what changed."""
    walls = _WallIndex(building.walls)
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
        conflicts = [hit_id for hit_id, _ in sorted(hits, key=lambda h: -h[1])]
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


def find_conflicts(building: Building) -> list[tuple[str, str, float]]:
    """(furniture id, wall/furniture id, depth) for every violation of the furniture rule."""
    walls = _WallIndex(building.walls)
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


def _gap_findings(b: _Builder) -> None:
    """Holes in the facade: a wall that stops in mid-air while its line runs into another wall a little
    further on, with the building's outside on one side of the hole. Usually a wall piece (or the entrance
    door, or glazing) the drawing or the parser lost; sometimes a real open porch or terrace."""
    walls = b.building.walls
    hull = _hull([corner for _, box in wall_boxes(walls) for corner in _corners(box)])
    seen: set[tuple] = set()
    for wall in walls:
        if wall.length <= 0:
            continue
        d = unit(sub(wall.end, wall.start))
        for point, outward in ((wall.end, d), (wall.start, scale(d, -1.0))):
            if _connected(point, wall, walls):
                continue
            hit: tuple[float, Wall, Point2] | None = None
            for other in walls:
                if other is wall or other.length <= 0:
                    continue
                q = line_intersection(point, add(point, outward), other.start, other.end)
                if q is None:
                    continue
                along = project_param(q, other.start, other.end)
                ahead = dot(sub(q, point), outward)
                if (
                    -other.thickness <= along <= other.length + other.thickness
                    and 0.05 < ahead
                    and (hit is None or ahead < hit[0])
                ):
                    hit = (ahead, other, q)
            if hit is None:
                continue
            ahead, other, q = hit
            gap = ahead - other.thickness / 2
            if not FACADE_GAP[0] <= gap <= FACADE_GAP[1]:
                continue
            key = tuple(sorted((tuple(round(v, 1) for v in point), tuple(round(v, 1) for v in q))))
            if key in seen:
                continue
            seen.add(key)
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
    regions = _regions(building, [box for _, box in wall_boxes(building.walls)])
    if regions is None:
        if building.walls:
            notes.append("The drawing is too large for the room checks (enclosed spaces, interior windows).")
    else:
        spaces = _space_graph(building, regions)
        _space_findings(b, spaces)
        _window_findings(b, spaces)
    _outside_findings(b)
    _, fixes = settle(building)
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
    building, fixes = settle(building, remove)
    for fix in fixes:
        changes.append(
            CheckChange(target_ids=[fix.item_id], summary=fix.summary, source="ai" if fix.decided else "guardrails")
        )
    return Resolution(building=building, findings=decided, changes=changes, notes=notes)


def enforce(building: Building) -> tuple[Building, list[FurnitureFix]]:
    """The hard rule only (after manual edits): returns the building unchanged when nothing conflicts."""
    if not find_conflicts(building):
        return building, []
    return settle(building)
