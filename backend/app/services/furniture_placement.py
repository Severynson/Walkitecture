"""Place furniture blocks as library models without an LLM (ARCHITECTURE.md, module 2).

Rules:
* The model's front faces away from the wall it stands against ("furniture is always directed away
  from the wall by its front side"): of the block's four sides (from the INSERT rotation), the one
  closest to a wall running along it is the back. Free-standing items face away from the nearest wall.
* Exception: a chair standing within reach of a table or desk faces that table/desk instead.
* The footprint (width across the facing direction, depth along it) comes from the block's actual
  geometry in the drawing; height comes from the library model's typical real-world size.
"""

import math
from dataclasses import dataclass

from app.schemas.building import FurnitureItem, Point2, Wall
from app.services.furniture_catalog import CATALOG, choose_model
from app.services.geometry import add, dot, left_normal, point_segment_distance, scale, sub, unit

MAX_FOOTPRINT = 5.0  # m: anything larger is not a piece of furniture
MIN_FOOTPRINT = 0.1  # m: smaller footprints fall back to the model's typical size
CHAIR_TO_TABLE_REACH = 1.2  # m between chair center and table footprint edge
BACK_TO_WALL_REACH = 0.6  # m between a furniture side and a wall face for "back against the wall"


@dataclass
class FurnitureCandidate:
    block: str
    layer: str
    category: str | None
    matched_by: str  # block_name | layer | llm
    points: list[Point2]  # plan coordinates (m)
    round_shape: bool = False
    rotation: float = 0.0  # INSERT rotation (degrees): gives the block's own side directions

    @property
    def size(self) -> tuple[float, float]:
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        return (max(xs) - min(xs), max(ys) - min(ys))

    @property
    def local_size(self) -> tuple[float, float]:
        """Footprint measured along the block's own axes (rotation-independent)."""
        r = math.radians(self.rotation)
        sizes = []
        for axis in ((math.cos(r), math.sin(r)), (-math.sin(r), math.cos(r))):
            values = [dot(p, axis) for p in self.points]
            sizes.append(max(values) - min(values))
        return (sizes[0], sizes[1])

    @property
    def center(self) -> Point2:
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        return ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)


def facing_away_from_nearest_wall(center: Point2, walls: list[Wall]) -> Point2:
    best: tuple[float, Wall] | None = None
    for wall in walls:
        d = point_segment_distance(center, wall.start, wall.end)
        if best is None or d < best[0]:
            best = (d, wall)
    if best is None:
        return (0.0, 1.0)
    wall = best[1]
    direction = unit(sub(wall.end, wall.start))
    t = max(0.0, min(wall.length, dot(sub(center, wall.start), direction)))
    closest = add(wall.start, scale(direction, t))
    away = unit(sub(center, closest))
    if away == (0.0, 0.0):  # center exactly on the wall line
        away = left_normal(wall.start, wall.end)
    return away


SQUARE_ASPECT = 1.25  # width/depth ratios within this factor of 1 count as "square" (orientation-agnostic)


def preferred_aspect(category: str | None) -> float:
    """Typical width/depth of the library models in a category: > 1 means the front is a long side
    (closet, sofa, desk), < 1 means the back is a short side (bed, toilet), ~1 means either."""
    sizes = [m.real_size for m in CATALOG if m.category == category]
    if not sizes:
        return 1.0
    return math.exp(sum(math.log(w / d) for w, _, d in sizes) / len(sizes))


def _shape_fits(width: float, depth: float, aspect: float) -> bool:
    """Does a footprint (width across the facing, depth along it) have the model's long axis the right way?"""
    if 1 / SQUARE_ASPECT <= aspect <= SQUARE_ASPECT or depth <= 0 or width <= 0:
        return True
    if 1 / SQUARE_ASPECT <= width / depth <= SQUARE_ASPECT:
        return True  # (nearly) square block: any side may be the back
    return (width >= depth) == (aspect >= 1)


def facing_from_back_wall(
    points: list[Point2], rotation_deg: float, walls: list[Wall], category: str | None = None
) -> Point2 | None:
    """Find which of the block's four sides sits against a wall; the front faces the opposite way.

    In a corner several sides touch walls. The back is then the side that gives the footprint the
    model's proportions (a wardrobe's back is a long side, a bed's head a short one); only among those
    does the closest wall win. Without that, a 1.6 x 0.4 m wardrobe could be turned into a
    0.4 m wide, 1.6 m deep one.
    """
    r = math.radians(rotation_deg)
    u, v = (math.cos(r), math.sin(r)), (-math.sin(r), math.cos(r))
    center: Point2 = (0.0, 0.0)
    halves = {}
    for axis in (u, v):
        values = [dot(p, axis) for p in points]
        halves[axis] = (max(values) - min(values)) / 2
        center = add(center, scale(axis, (max(values) + min(values)) / 2))
    aspect = preferred_aspect(category)
    best: tuple[tuple[int, float], Point2] | None = None
    for axis, other in ((u, v), (v, u)):
        width, depth = 2 * halves[other], 2 * halves[axis]
        rank = 0 if _shape_fits(width, depth, aspect) else 1
        for side in (axis, scale(axis, -1.0)):
            midpoint = add(center, scale(side, halves[axis]))
            for wall in walls:
                if abs(dot(unit(sub(wall.end, wall.start)), side)) > 0.3:
                    continue  # the wall must run along this side
                gap = point_segment_distance(midpoint, wall.start, wall.end) - wall.thickness / 2
                if gap <= BACK_TO_WALL_REACH and (best is None or (rank, gap) < best[0]):
                    best = ((rank, gap), side)
    if best is None or best[0][0] == 1:
        return None  # no wall behind a correctly proportioned side: placed as free-standing instead
    return (-best[1][0] + 0.0, -best[1][1] + 0.0)


def _footprint(points: list[Point2], facing: Point2) -> tuple[Point2, float, float]:
    """(center, width across facing, depth along facing) of the points, aligned to `facing`."""
    tangent = (-facing[1], facing[0])
    ts = [dot(p, tangent) for p in points]
    fs = [dot(p, facing) for p in points]
    t_mid, f_mid = (max(ts) + min(ts)) / 2, (max(fs) + min(fs)) / 2
    center = add(scale(tangent, t_mid), scale(facing, f_mid))
    return center, max(ts) - min(ts), max(fs) - min(fs)


def _item(candidate: FurnitureCandidate, facing: Point2, index: int, warnings: list[str]) -> FurnitureItem | None:
    center, width, depth = _footprint(candidate.points, facing)
    if max(width, depth) > MAX_FOOTPRINT:
        warnings.append(f"Block {candidate.block!r} ({width:.1f} x {depth:.1f} m) is too large for furniture; skipped.")
        return None
    model = choose_model(candidate.category or "", width, depth, candidate.round_shape)
    if model is None:
        return None
    if width < MIN_FOOTPRINT or depth < MIN_FOOTPRINT:
        width, depth = model.real_size[0], model.real_size[2]
    return FurnitureItem(
        id=f"furniture_{index}",
        category=model.category,
        model=model.file,
        source_block=candidate.block,
        matched_by=candidate.matched_by,
        position=(round(center[0], 4), round(center[1], 4)),
        facing=(round(facing[0], 4) + 0.0, round(facing[1], 4) + 0.0),
        width=round(width, 4),
        depth=round(depth, 4),
        height=model.real_size[1],
    )


def place_furniture(
    candidates: list[FurnitureCandidate], walls: list[Wall], warnings: list[str], start_index: int = 1
) -> list[FurnitureItem]:
    known_categories = {m.category for m in CATALOG}
    usable = [c for c in candidates if c.category in known_categories and c.points]
    unmatched = sorted({c.block for c in candidates if c.category not in known_categories})
    if unmatched:
        shown = ", ".join(repr(b) for b in unmatched[:12])
        more = f" (+{len(unmatched) - 12} more)" if len(unmatched) > 12 else ""
        warnings.append(f"No library model for furniture blocks {shown}{more}; they were left out.")

    items: list[FurnitureItem] = []
    index = start_index
    # Tables and desks first, so chairs can turn toward them.
    for candidate in sorted(usable, key=lambda c: c.category == "chair"):
        facing = facing_from_back_wall(
            candidate.points, candidate.rotation, walls, candidate.category
        ) or _free_standing_facing(candidate, walls)
        if candidate.category == "chair":
            facing = _toward_nearest_table(candidate.center, items) or facing
        item = _item(candidate, facing, index, warnings)
        if item:
            items.append(item)
            index += 1
    return items


def _free_standing_facing(candidate: FurnitureCandidate, walls: list[Wall]) -> Point2:
    """No wall behind a correctly proportioned side: keep the model's proportions as drawn (front on a
    long side for sofas/desks/closets, on a short side for beds/toilets) and face away from the nearest wall."""
    away = facing_away_from_nearest_wall(candidate.center, walls)
    aspect = preferred_aspect(candidate.category)
    if candidate.category == "chair" or 1 / SQUARE_ASPECT <= aspect <= SQUARE_ASPECT:
        return away
    r = math.radians(candidate.rotation)
    u, v = (math.cos(r), math.sin(r)), (-math.sin(r), math.cos(r))
    size_u, size_v = candidate.local_size
    long_axis, short_axis = (u, v) if size_u >= size_v else (v, u)
    axis = short_axis if aspect >= 1 else long_axis  # facing runs along the depth
    return axis if dot(axis, away) >= 0 else (-axis[0] + 0.0, -axis[1] + 0.0)


def _toward_nearest_table(center: Point2, placed: list[FurnitureItem]) -> Point2 | None:
    best: tuple[float, FurnitureItem] | None = None
    for item in placed:
        if item.category not in ("table", "desk"):
            continue
        edge_distance = math.dist(center, item.position) - min(item.width, item.depth) / 2
        if edge_distance <= CHAIR_TO_TABLE_REACH and (best is None or edge_distance < best[0]):
            best = (edge_distance, item)
    if best is None:
        return None
    direction = unit(sub(best[1].position, center))
    return direction if direction != (0.0, 0.0) else None
