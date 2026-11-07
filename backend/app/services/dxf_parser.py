"""DXF -> canonical building model.

Drawing convention (layer names are configurable, case-insensitive glob patterns):

* Walls   - LINE / LWPOLYLINE / POLYLINE on wall layers. In 'centerline' style every line is a wall
            centerline; in 'double_line' style walls are pairs of parallel face lines and thickness is
            inferred from the gap.
* Openings - any entities (lines, polylines, arcs, block INSERTs) on window / door layers. Each
            top-level entity becomes one opening, snapped onto the nearest wall; overlapping pieces of
            the same type on the same wall are merged (e.g. a window drawn as three parallel lines).
* Rooms   - closed polylines and TEXT / MTEXT labels on room layers.

If no wall-layer geometry exists, all unclassified linework is treated as walls (with a warning),
so arbitrary simple drawings still produce something walkable.
"""

import fnmatch
import io
import math
from collections import Counter
from dataclasses import dataclass, field

from ezdxf import recover
from ezdxf.disassemble import recursive_decompose, to_primitives
from ezdxf.entities import DXFGraphic
from ezdxf.enums import InsertUnits
from ezdxf.lldxf.const import DXFError
from ezdxf.units import conversion_factor

from app.config import Settings
from app.schemas.building import Building, Opening, Point2, Room, Wall
from app.schemas.parse import ParseOptions
from app.services.geometry import (
    MIN_HEADER,
    MIN_OPENING_WIDTH,
    MIN_WALL_LENGTH,
    add,
    cross,
    dot,
    line_intersection,
    point_in_polygon,
    point_segment_distance,
    polygon_centroid,
    project_param,
    scale,
    sub,
    unit,
    validate_building,
)

UNIT_FACTORS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}
LINEAR_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}
TEXT_TYPES = {"TEXT", "MTEXT"}
SKIP_IN_OPENINGS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION"}
MAX_INSERT_DEPTH = 8
SNAP_TOLERANCE = 0.01  # m: endpoints closer than this are merged
MAX_BRIDGE_GAP = 4.0  # m: largest wall gap that an opening may bridge
PARALLEL_SIN = math.sin(math.radians(2.0))


class ParseError(Exception):
    """The drawing could not be turned into a building model."""


@dataclass
class ParseResult:
    building: Building
    warnings: list[str]
    stats: dict


@dataclass
class _Centerline:
    a: Point2
    b: Point2
    thickness: float

    @property
    def length(self) -> float:
        return math.dist(self.a, self.b)


@dataclass
class _Collected:
    walls: list[DXFGraphic] = field(default_factory=list)
    openings: list[tuple[str, DXFGraphic]] = field(default_factory=list)
    room_shapes: list[DXFGraphic] = field(default_factory=list)
    room_labels: list[DXFGraphic] = field(default_factory=list)
    unclassified_lines: list[DXFGraphic] = field(default_factory=list)
    layer_counts: Counter = field(default_factory=Counter)


@dataclass
class _Layers:
    wall: list[str]
    window: list[str]
    door: list[str]
    room: list[str]

    def classify(self, layer: str) -> str | None:
        name = layer.upper()
        for kind, patterns in (("wall", self.wall), ("window", self.window), ("door", self.door), ("room", self.room)):
            if any(fnmatch.fnmatchcase(name, p.upper()) for p in patterns):
                return kind
        return None


# --- Public entry point ----------------------------------------------------------------


def parse_dxf(data: bytes, options: ParseOptions, settings: Settings) -> ParseResult:
    try:
        doc, _auditor = recover.read(io.BytesIO(data))
    except (DXFError, OSError, ValueError) as exc:
        raise ParseError(f"Could not read DXF: {exc}") from exc

    warnings: list[str] = []
    layers = _Layers(
        wall=options.wall_layers or settings.wall_layers,
        window=options.window_layers or settings.window_layers,
        door=options.door_layers or settings.door_layers,
        room=options.room_layers or settings.room_layers,
    )
    collected = _Collected()
    _collect(doc.modelspace(), layers, collected, warnings)

    wall_entities = collected.walls
    if not wall_entities:
        if not collected.unclassified_lines:
            found = ", ".join(sorted(collected.layer_counts)) or "none"
            raise ParseError(
                f"No wall geometry found. Expected lines on layers matching {layers.wall}; layers in file: {found}."
            )
        used = sorted({e.dxf.layer for e in collected.unclassified_lines})
        warnings.append(
            f"No entities on wall layers {layers.wall}; treating {len(collected.unclassified_lines)} line entities "
            f"on layers {used} as walls."
        )
        wall_entities = collected.unclassified_lines

    raw_segments: list[tuple[Point2, Point2]] = []
    arc_count = 0
    for entity in wall_entities:
        segs, had_arc = _segments(entity)
        raw_segments.extend(segs)
        arc_count += had_arc
    if arc_count:
        warnings.append(f"{arc_count} wall polylines contain arc segments; arcs were approximated by straight chords.")

    factor, units_label = _unit_factor(doc.header.get("$INSUNITS", 0), options, raw_segments, warnings)

    meters = [(scale(a, factor), scale(b, factor)) for a, b in raw_segments]
    meters = [(a, b) for a, b in meters if math.dist(a, b) >= 0.02]
    if not meters:
        raise ParseError("All wall segments are degenerate after unit conversion. Check the drawing units.")
    origin = (min(min(a[0], b[0]) for a, b in meters), min(min(a[1], b[1]) for a, b in meters))

    def to_plan(p: Point2) -> Point2:
        return (p[0] * factor - origin[0], p[1] * factor - origin[1])

    segments = [(sub(a, origin), sub(b, origin)) for a, b in meters]
    opening_points = [(kind, [to_plan(p) for p in _entity_points(e)]) for kind, e in collected.openings]
    opening_points = [(kind, pts) for kind, pts in opening_points if pts]

    thickness = options.wall_thickness or settings.default_wall_thickness
    if options.wall_style == "double_line":
        centerlines = _pair_double_lines(segments, options.min_wall_thickness, options.max_wall_thickness, warnings)
        if not centerlines:
            raise ParseError(
                "Double-line mode found no parallel wall-face pairs. Try wall_style='centerline' or adjust "
                "min/max wall thickness."
            )
    else:
        centerlines = [_Centerline(a, b, thickness) for a, b in segments]

    _extend_to_corners(centerlines)
    centerlines = _bridge_openings(centerlines, [pts for _, pts in opening_points])
    _snap_endpoints(centerlines)
    centerlines = [c for c in centerlines if c.length >= MIN_WALL_LENGTH]

    ceiling = options.ceiling_height or settings.default_ceiling_height
    walls = [
        Wall(id=f"wall_{i}", start=_r(c.a), end=_r(c.b), thickness=round(c.thickness, 4), height=ceiling)
        for i, c in enumerate(centerlines, start=1)
    ]
    _place_openings(walls, opening_points, options, settings, warnings)
    rooms = _build_rooms(collected, to_plan)

    building = Building(ceiling_height=ceiling, walls=walls, rooms=rooms)
    for problem in validate_building(building):
        warnings.append(f"Validation: {problem.message}")

    xs = [p[0] for w in walls for p in (w.start, w.end)]
    ys = [p[1] for w in walls for p in (w.start, w.end)]
    stats = {
        "detectedUnits": units_label,
        "unitFactor": factor,
        "wallStyle": options.wall_style,
        "extent": [round(max(xs, default=0), 3), round(max(ys, default=0), 3)],
        "wallCount": len(walls),
        "windowCount": sum(o.type == "window" for w in walls for o in w.openings),
        "doorCount": sum(o.type == "door" for w in walls for o in w.openings),
        "roomCount": len(rooms),
        "layers": dict(sorted(collected.layer_counts.items())),
    }
    return ParseResult(building=building, warnings=warnings, stats=stats)


# --- Entity collection -------------------------------------------------------------------


def _collect(
    entities, layers: _Layers, out: _Collected, warnings: list[str], inherited: str | None = None, depth: int = 0
) -> None:
    for entity in entities:
        layer = entity.dxf.get("layer", "0")
        if layer == "0" and inherited:
            layer = inherited
            entity.dxf.layer = inherited
        out.layer_counts[layer] += 1
        kind = layers.classify(layer)
        dxftype = entity.dxftype()

        if dxftype == "INSERT":
            if kind in ("window", "door"):
                out.openings.append((kind, entity))
            elif depth < MAX_INSERT_DEPTH:
                try:
                    children = list(entity.virtual_entities())
                except Exception as exc:  # malformed blocks should not abort the whole parse
                    warnings.append(f"Skipped block reference {entity.dxf.get('name', '?')}: {exc}")
                    continue
                _collect(children, layers, out, warnings, inherited=layer, depth=depth + 1)
            continue

        if kind == "wall" and dxftype in LINEAR_TYPES:
            out.walls.append(entity)
        elif kind in ("window", "door"):
            out.openings.append((kind, entity))
        elif kind == "room":
            if dxftype in TEXT_TYPES:
                out.room_labels.append(entity)
            elif dxftype in ("LWPOLYLINE", "POLYLINE") and entity.is_closed:
                out.room_shapes.append(entity)
        elif kind is None and dxftype in LINEAR_TYPES:
            out.unclassified_lines.append(entity)


def _segments(entity: DXFGraphic) -> tuple[list[tuple[Point2, Point2]], bool]:
    dxftype = entity.dxftype()
    if dxftype == "LINE":
        s, e = entity.dxf.start, entity.dxf.end
        return [((s.x, s.y), (e.x, e.y))], False
    if dxftype == "LWPOLYLINE":
        points = [(v.x, v.y) for v in entity.vertices_in_wcs()]
        closed, has_arc = entity.closed, entity.has_arc
    else:  # POLYLINE
        if entity.is_poly_face_mesh or entity.is_polygon_mesh:
            return [], False
        points = [(v.x, v.y) for v in entity.points_in_wcs()]
        closed, has_arc = entity.is_closed, entity.has_arc
    if closed and len(points) > 2:
        points.append(points[0])
    return list(zip(points, points[1:], strict=False)), has_arc


def _entity_points(entity: DXFGraphic) -> list[Point2]:
    parts = [e for e in recursive_decompose([entity]) if e.dxftype() not in SKIP_IN_OPENINGS]
    points: list[Point2] = []
    for primitive in to_primitives(parts):
        points.extend((v.x, v.y) for v in primitive.vertices())
    return points


def _unit_factor(
    insunits: int, options: ParseOptions, segments: list[tuple[Point2, Point2]], warnings: list[str]
) -> tuple[float, str]:
    if options.scale:
        return options.scale, "custom scale"
    if options.units != "auto":
        return UNIT_FACTORS[options.units], options.units
    if insunits:
        try:
            unit_enum = InsertUnits(insunits)
            return conversion_factor(unit_enum, InsertUnits.Meters), unit_enum.name.lower()
        except (ValueError, KeyError, ZeroDivisionError):
            warnings.append(f"Unsupported $INSUNITS value {insunits}; guessing units from drawing size.")

    xs = [p[0] for seg in segments for p in seg]
    ys = [p[1] for seg in segments for p in seg]
    extent = max(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0
    guess = "mm" if extent >= 2000 else "cm" if extent >= 200 else "m"
    warnings.append(
        f"Drawing has no units ($INSUNITS unset); assumed '{guess}' from its {extent:.0f}-unit extent. "
        "Pass 'units' or 'scale' to override."
    )
    return UNIT_FACTORS[guess], f"{guess} (guessed)"


# --- Wall reconstruction -----------------------------------------------------------------


def _pair_double_lines(
    segments: list[tuple[Point2, Point2]], min_t: float, max_t: float, warnings: list[str]
) -> list[_Centerline]:
    """Pair parallel face lines into centerlines. A long face may pair with several shorter ones."""
    candidates = []
    for i, (a, b) in enumerate(segments):
        di, li = unit(sub(b, a)), math.dist(a, b)
        for j in range(i + 1, len(segments)):
            c, d = segments[j]
            dj = unit(sub(d, c))
            if abs(cross(di, dj)) > PARALLEL_SIN:
                continue
            dist_c, dist_d = cross(di, sub(c, a)), cross(di, sub(d, a))
            if dist_c * dist_d <= 0 or abs(dist_c - dist_d) > 0.02:
                continue
            gap = (abs(dist_c) + abs(dist_d)) / 2
            if not min_t <= gap <= max_t:
                continue
            tc, td = dot(sub(c, a), di), dot(sub(d, a), di)
            lo, hi = max(0.0, min(tc, td)), min(li, max(tc, td))
            if hi - lo < max(MIN_WALL_LENGTH, gap):
                continue
            candidates.append((gap, -(hi - lo), i, j, lo, hi, math.copysign(1.0, dist_c)))

    candidates.sort()
    used: list[list[tuple[float, float]]] = [[] for _ in segments]
    centerlines: list[_Centerline] = []
    for gap, _neg_overlap, i, j, lo, hi, side in candidates:
        a, b = segments[i]
        c, d = segments[j]
        di, dj = unit(sub(b, a)), unit(sub(d, c))
        pa, pb = add(a, scale(di, lo)), add(a, scale(di, hi))
        lo_j, hi_j = sorted((dot(sub(pa, c), dj), dot(sub(pb, c), dj)))
        if _overlaps(used[i], lo, hi) or _overlaps(used[j], lo_j, hi_j):
            continue
        used[i].append((lo, hi))
        used[j].append((lo_j, hi_j))
        shift = scale((-di[1], di[0]), side * gap / 2)
        centerlines.append(_Centerline(add(pa, shift), add(pb, shift), gap))

    unpaired = sum(1 for (a, b), u in zip(segments, used, strict=True) if not u and math.dist(a, b) > max_t)
    if unpaired:
        warnings.append(f"{unpaired} wall lines had no parallel partner within {min_t}-{max_t} m and were ignored.")
    return centerlines


def _overlaps(intervals: list[tuple[float, float]], lo: float, hi: float) -> bool:
    return any(lo < h - 0.01 and hi > l + 0.01 for l, h in intervals)


def _extend_to_corners(centerlines: list[_Centerline]) -> None:
    """Move each wall end to the nearest intersection with another wall's line, if it is close.

    Joins L-corners and T-junctions whose centerlines stop short (typical for double-line drawings).
    """
    for wall in centerlines:
        for end in ("a", "b"):
            point = getattr(wall, end)
            best: tuple[float, Point2] | None = None
            for other in centerlines:
                if other is wall:
                    continue
                reach = max(wall.thickness, other.thickness)
                hit = line_intersection(wall.a, wall.b, other.a, other.b)
                if hit is None:
                    continue
                distance = math.dist(point, hit)
                if distance < 1e-6 or distance > reach:
                    continue
                if point_segment_distance(hit, other.a, other.b) > reach:
                    continue
                # Never collapse the wall: the hit must not lie past the opposite end.
                if project_param(hit, wall.a, wall.b) <= 0 and end == "b":
                    continue
                if project_param(hit, wall.b, wall.a) <= 0 and end == "a":
                    continue
                if best is None or distance < best[0]:
                    best = (distance, hit)
            if best:
                setattr(wall, end, best[1])


def _bridge_openings(centerlines: list[_Centerline], opening_sets: list[list[Point2]]) -> list[_Centerline]:
    """Merge collinear wall pieces whose gap contains a window/door (walls are often broken at openings)."""
    walls = list(centerlines)
    merged = True
    while merged:
        merged = False
        for i in range(len(walls)):
            for j in range(i + 1, len(walls)):
                joined = _try_bridge(walls[i], walls[j], opening_sets)
                if joined:
                    walls[i] = joined
                    del walls[j]
                    merged = True
                    break
            if merged:
                break
    return walls


def _try_bridge(w1: _Centerline, w2: _Centerline, opening_sets: list[list[Point2]]) -> _Centerline | None:
    if abs(w1.thickness - w2.thickness) > 0.02:
        return None
    d1 = unit(sub(w1.b, w1.a))
    if abs(cross(d1, unit(sub(w2.b, w2.a)))) > PARALLEL_SIN:
        return None
    tol = max(0.02, w1.thickness / 4)
    if abs(cross(d1, sub(w2.a, w1.a))) > tol or abs(cross(d1, sub(w2.b, w1.a))) > tol:
        return None
    t2 = sorted((dot(sub(w2.a, w1.a), d1), dot(sub(w2.b, w1.a), d1)))
    l1 = w1.length
    if t2[0] >= l1:
        gap_lo, gap_hi = l1, t2[0]
    elif t2[1] <= 0:
        gap_lo, gap_hi = t2[1], 0.0
    else:
        return None  # overlapping collinear pieces: leave as-is
    if not 0.05 <= gap_hi - gap_lo <= MAX_BRIDGE_GAP:
        return None
    reach = max(w1.thickness, 0.3)
    for points in opening_sets:
        ts = [dot(sub(p, w1.a), d1) for p in points if abs(cross(d1, sub(p, w1.a))) <= reach]
        # The opening must overlap the gap (it usually spans exactly jamb to jamb).
        if ts and max(ts) > gap_lo + 0.01 and min(ts) < gap_hi - 0.01:
            lo, hi = min(0.0, t2[0]), max(l1, t2[1])
            return _Centerline(add(w1.a, scale(d1, lo)), add(w1.a, scale(d1, hi)), w1.thickness)
    return None


def _snap_endpoints(centerlines: list[_Centerline]) -> None:
    refs = [(c, end) for c in centerlines for end in ("a", "b")]
    clusters: list[list[tuple[_Centerline, str]]] = []
    for ref in refs:
        point = getattr(*ref)
        for cluster in clusters:
            if math.dist(point, getattr(*cluster[0])) <= SNAP_TOLERANCE:
                cluster.append(ref)
                break
        else:
            clusters.append([ref])
    for cluster in clusters:
        if len(cluster) > 1:
            xs, ys = zip(*(getattr(*ref) for ref in cluster), strict=True)
            mean = (sum(xs) / len(xs), sum(ys) / len(ys))
            for c, end in cluster:
                setattr(c, end, mean)


# --- Openings and rooms ------------------------------------------------------------------


def _place_openings(
    walls: list[Wall],
    opening_sets: list[tuple[str, list[Point2]]],
    options: ParseOptions,
    settings: Settings,
    warnings: list[str],
) -> None:
    spans: dict[str, list[tuple[str, float, float]]] = {w.id: [] for w in walls}
    skipped = 0
    for kind, points in opening_sets:
        host = _host_wall(points, walls)
        if host is None:
            skipped += 1
            continue
        ts = [project_param(p, host.start, host.end) for p in points]
        lo, hi = max(0.0, min(ts)), min(host.length, max(ts))
        if hi - lo < MIN_OPENING_WIDTH:
            skipped += 1
            continue
        spans[host.id].append((kind, lo, hi))
    if skipped:
        warnings.append(f"{skipped} window/door entities were not on any wall (or too narrow) and were skipped.")

    counters = {"window": 0, "door": 0}
    window_h = options.window_height or settings.default_window_height
    sill = options.window_sill_height if options.window_sill_height is not None else settings.default_window_sill_height
    door_h = options.door_height or settings.default_door_height
    for wall in walls:
        for kind, lo, hi in _merge_spans(spans[wall.id], wall.id, warnings):
            counters[kind] += 1
            max_top = wall.height - MIN_HEADER
            if kind == "window":
                height, sill_h = min(window_h, max(max_top - sill, 0.3)), min(sill, max_top - 0.3)
            else:
                height, sill_h = min(door_h, max_top), 0.0
            wall.openings.append(
                Opening(
                    id=f"{kind}_{counters[kind]}",
                    type=kind,
                    offset=round(lo, 4),
                    width=round(hi - lo, 4),
                    height=round(height, 4),
                    sill_height=round(sill_h, 4),
                )
            )


def _host_wall(points: list[Point2], walls: list[Wall]) -> Wall | None:
    best: tuple[int, float, Wall] | None = None
    for wall in walls:
        reach = max(wall.thickness, 0.3)
        distances = [point_segment_distance(p, wall.start, wall.end) for p in points]
        near = sum(d <= reach for d in distances)
        if near == 0:
            continue
        score = (near, -sum(distances) / len(distances))
        if best is None or score > best[:2]:
            best = (*score, wall)
    return best[2] if best else None


def _merge_spans(spans: list[tuple[str, float, float]], wall_id: str, warnings: list[str]):
    merged: list[tuple[str, float, float]] = []
    for kind, lo, hi in sorted(spans, key=lambda s: s[1]):
        if merged and lo < merged[-1][2] - 1e-4:
            prev_kind, prev_lo, prev_hi = merged[-1]
            if prev_kind == kind:
                merged[-1] = (kind, prev_lo, max(prev_hi, hi))
            else:
                warnings.append(f"Overlapping {prev_kind} and {kind} on {wall_id}; kept the {prev_kind}.")
            continue
        merged.append((kind, lo, hi))
    return merged


def _build_rooms(collected: _Collected, to_plan) -> list[Room]:
    shapes: list[list[Point2]] = []
    for entity in collected.room_shapes:
        if entity.dxftype() == "LWPOLYLINE":
            pts = [to_plan((v.x, v.y)) for v in entity.vertices_in_wcs()]
        else:
            pts = [to_plan((v.x, v.y)) for v in entity.points_in_wcs()]
        if len(pts) >= 3:
            shapes.append([_r(p) for p in pts])

    labels: list[tuple[str, Point2]] = []
    for entity in collected.room_labels:
        text = entity.plain_text() if entity.dxftype() == "MTEXT" else entity.dxf.get("text", "")
        text = " ".join(str(text).split())[:80]
        if text:
            insert = entity.dxf.insert
            labels.append((text, to_plan((insert.x, insert.y))))

    rooms: list[Room] = []
    unused = list(labels)
    for polygon in shapes:
        label = next((lab for lab in unused if point_in_polygon(lab[1], polygon)), None)
        if label:
            unused.remove(label)
        rooms.append(
            Room(
                id=f"room_{len(rooms) + 1}",
                name=label[0] if label else f"Room {len(rooms) + 1}",
                polygon=polygon,
                label_position=_r(label[1]) if label else _r(polygon_centroid(polygon)),
            )
        )
    for text, position in unused:
        rooms.append(Room(id=f"room_{len(rooms) + 1}", name=text, label_position=_r(position)))
    return rooms


def _r(p: Point2) -> Point2:
    return (round(p[0], 4) + 0.0, round(p[1], 4) + 0.0)
