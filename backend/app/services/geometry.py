"""Deterministic 2D geometry helpers and building validation rules (all values in meters)."""

import math
from collections import Counter

from app.schemas.base import CamelModel
from app.schemas.building import Building, Point2, Wall

EPS = 1e-6

# --- Sanity limits ---------------------------------------------------------------------
MIN_WALL_LENGTH = 0.1
MIN_WALL_THICKNESS = 0.02
MAX_WALL_THICKNESS = 1.0
MIN_WALL_HEIGHT = 1.0
MAX_WALL_HEIGHT = 10.0
MIN_CEILING_HEIGHT = 2.1
MAX_CEILING_HEIGHT = 6.0
MIN_OPENING_WIDTH = 0.2
MAX_OPENING_WIDTH = 12.0
MIN_OPENING_HEIGHT = 0.2
MIN_HEADER = 0.05  # solid wall that must remain above every opening
MAX_WALL_MOVE = 5.0


class Problem(CamelModel):
    """A single validation failure, safe to show to end users."""

    code: str
    message: str
    target_id: str | None = None


# --- Vector helpers --------------------------------------------------------------------


def sub(a: Point2, b: Point2) -> Point2:
    return (a[0] - b[0], a[1] - b[1])


def add(a: Point2, b: Point2) -> Point2:
    return (a[0] + b[0], a[1] + b[1])


def scale(a: Point2, k: float) -> Point2:
    return (a[0] * k, a[1] * k)


def dot(a: Point2, b: Point2) -> float:
    return a[0] * b[0] + a[1] * b[1]


def cross(a: Point2, b: Point2) -> float:
    return a[0] * b[1] - a[1] * b[0]


def unit(a: Point2) -> Point2:
    n = math.hypot(*a)
    return (0.0, 0.0) if n < EPS else (a[0] / n, a[1] / n)


def left_normal(start: Point2, end: Point2) -> Point2:
    dx, dy = unit(sub(end, start))
    return (-dy, dx)


def project_param(p: Point2, a: Point2, b: Point2) -> float:
    """Distance of p's projection along segment a->b, measured from a (unclamped)."""
    return dot(sub(p, a), unit(sub(b, a)))


def point_segment_distance(p: Point2, a: Point2, b: Point2) -> float:
    ab = sub(b, a)
    length_sq = dot(ab, ab)
    if length_sq < EPS:
        return math.dist(p, a)
    t = max(0.0, min(1.0, dot(sub(p, a), ab) / length_sq))
    return math.dist(p, add(a, scale(ab, t)))


def segments_intersect(a: Point2, b: Point2, c: Point2, d: Point2) -> bool:
    d1, d2 = cross(sub(b, a), sub(c, a)), cross(sub(b, a), sub(d, a))
    d3, d4 = cross(sub(d, c), sub(a, c)), cross(sub(d, c), sub(b, c))
    return (d1 * d2 < 0) and (d3 * d4 < 0)


def segment_distance(a: Point2, b: Point2, c: Point2, d: Point2) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def line_intersection(a: Point2, b: Point2, c: Point2, d: Point2) -> Point2 | None:
    """Intersection of the infinite lines a-b and c-d, or None when (nearly) parallel."""
    r, s = sub(b, a), sub(d, c)
    denom = cross(r, s)
    if abs(denom) < EPS:
        return None
    t = cross(sub(c, a), s) / denom
    return add(a, scale(r, t))


def point_in_polygon(p: Point2, polygon: list[Point2]) -> bool:
    inside = False
    n = len(polygon)
    for i in range(n):
        (x1, y1), (x2, y2) = polygon[i], polygon[(i + 1) % n]
        if (y1 > p[1]) != (y2 > p[1]):
            x_cross = x1 + (p[1] - y1) * (x2 - x1) / (y2 - y1)
            if p[0] < x_cross:
                inside = not inside
    return inside


def polygon_centroid(polygon: list[Point2]) -> Point2:
    xs, ys = zip(*polygon, strict=True)
    return (sum(xs) / len(xs), sum(ys) / len(ys))


# --- Topology ------------------------------------------------------------------------


def wall_connections(building: Building, wall: Wall) -> list[str]:
    """IDs of walls that touch `wall` (shared corners, T-junctions, or crossings)."""
    connected = []
    for other in building.walls:
        if other.id == wall.id:
            continue
        tol = (wall.thickness + other.thickness) / 2 + 0.01
        touches = (
            point_segment_distance(wall.start, other.start, other.end) <= tol
            or point_segment_distance(wall.end, other.start, other.end) <= tol
            or point_segment_distance(other.start, wall.start, wall.end) <= tol
            or point_segment_distance(other.end, wall.start, wall.end) <= tol
            or segments_intersect(wall.start, wall.end, other.start, other.end)
        )
        if touches:
            connected.append(other.id)
    return connected


# --- Validation ----------------------------------------------------------------------


def validate_wall(wall: Wall) -> list[Problem]:
    problems: list[Problem] = []
    length = wall.length
    if length < MIN_WALL_LENGTH:
        problems.append(
            Problem(code="wall_too_short", message=f"Wall {wall.id} is only {length:.3f} m long.", target_id=wall.id)
        )
    if not MIN_WALL_THICKNESS <= wall.thickness <= MAX_WALL_THICKNESS:
        problems.append(
            Problem(
                code="wall_thickness_out_of_range",
                message=f"Wall {wall.id} thickness {wall.thickness:.3f} m is outside "
                f"{MIN_WALL_THICKNESS}-{MAX_WALL_THICKNESS} m.",
                target_id=wall.id,
            )
        )
    if not MIN_WALL_HEIGHT <= wall.height <= MAX_WALL_HEIGHT:
        problems.append(
            Problem(
                code="wall_height_out_of_range",
                message=f"Wall {wall.id} height {wall.height:.2f} m is outside {MIN_WALL_HEIGHT}-{MAX_WALL_HEIGHT} m.",
                target_id=wall.id,
            )
        )

    for opening in wall.openings:
        label = f"{opening.type.capitalize()} {opening.id}"
        if not MIN_OPENING_WIDTH <= opening.width <= MAX_OPENING_WIDTH:
            problems.append(
                Problem(
                    code="opening_width_out_of_range",
                    message=f"{label} width {opening.width:.2f} m is outside "
                    f"{MIN_OPENING_WIDTH}-{MAX_OPENING_WIDTH} m.",
                    target_id=opening.id,
                )
            )
        if opening.height < MIN_OPENING_HEIGHT:
            problems.append(
                Problem(
                    code="opening_height_out_of_range",
                    message=f"{label} height {opening.height:.2f} m is below {MIN_OPENING_HEIGHT} m.",
                    target_id=opening.id,
                )
            )
        if opening.offset < -EPS or opening.offset + opening.width > length + 1e-4:
            problems.append(
                Problem(
                    code="opening_outside_wall",
                    message=f"{label} ({opening.width:.2f} m wide at offset {opening.offset:.2f} m) does not fit "
                    f"within wall {wall.id} ({length:.2f} m long).",
                    target_id=opening.id,
                )
            )
        if opening.top > wall.height - MIN_HEADER + 1e-4:
            problems.append(
                Problem(
                    code="opening_too_tall",
                    message=f"{label} top ({opening.top:.2f} m) leaves less than {MIN_HEADER} m of wall "
                    f"below the {wall.height:.2f} m ceiling.",
                    target_id=opening.id,
                )
            )

    ordered = sorted(wall.openings, key=lambda o: o.offset)
    for prev, nxt in zip(ordered, ordered[1:], strict=False):
        if nxt.offset < prev.offset + prev.width - 1e-4:
            problems.append(
                Problem(
                    code="openings_overlap",
                    message=f"Openings {prev.id} and {nxt.id} overlap on wall {wall.id}.",
                    target_id=nxt.id,
                )
            )
    return problems


def validate_building(building: Building) -> list[Problem]:
    problems: list[Problem] = []
    if not MIN_CEILING_HEIGHT <= building.ceiling_height <= MAX_CEILING_HEIGHT:
        problems.append(
            Problem(
                code="ceiling_height_out_of_range",
                message=f"Ceiling height {building.ceiling_height:.2f} m is outside "
                f"{MIN_CEILING_HEIGHT}-{MAX_CEILING_HEIGHT} m.",
            )
        )
    ids = [w.id for w in building.walls]
    ids += [o.id for w in building.walls for o in w.openings]
    ids += [r.id for r in building.rooms]
    for dup, count in Counter(ids).items():
        if count > 1:
            problems.append(Problem(code="duplicate_id", message=f"ID {dup} is used {count} times.", target_id=dup))
    for wall in building.walls:
        problems.extend(validate_wall(wall))
    return problems
