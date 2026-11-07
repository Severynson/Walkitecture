"""DXF -> canonical building model (walls with openings, rooms, furniture).

Entities are classified by `classify.DrawingClassifier` (layer names, block names, geometry), then:

* Walls   - wall-layer lines (plus top-level layer "0" linework when the wall layers are sparse,
            which is how the example call-center drawing is organized). 'auto' wall style detects
            double-line drawings; pairing takes the *widest* parallel gap first and absorbs any
            lines inside that band, so multi-leaf (cavity/plastered) walls become one wall.
* Openings - door/window blocks or linework, projected onto their host wall. Wall pieces broken at
            an opening (and small gaps at T-junctions) are merged back into one wall.
* Rooms   - closed outlines on room layers; names from text inside them (or room-like words).
* Furniture - furniture blocks matched to the GLB library and oriented away from the nearest wall.

Declared drawing units are sanity-checked against the building size (real files often lie).
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from ezdxf.disassemble import recursive_decompose, to_primitives
from ezdxf.entities import DXFGraphic
from ezdxf.enums import InsertUnits
from ezdxf.units import conversion_factor

from app.config import Settings
from app.schemas.building import Building, Opening, Point2, Room, Wall
from app.schemas.parse import ParseOptions
from app.services.classify import DrawingClassifier, is_closed_shape, polyline_points
from app.services.drawing_reader import DrawingReadError, read_dxf
from app.services.furniture_catalog import category_from_size
from app.services.furniture_placement import FurnitureCandidate, place_furniture
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
from app.services.layers import layer_patterns

UNIT_FACTORS = {"mm": 0.001, "cm": 0.01, "m": 1.0, "in": 0.0254, "ft": 0.3048}
UNIT_PREFERENCE = ("m", "mm", "cm", "in", "ft")  # tried in order when the header is implausible
PLAUSIBLE_EXTENT = (2.0, 300.0)  # meters across: a room ... a large building
LINEAR_TYPES = {"LINE", "LWPOLYLINE", "POLYLINE"}
TEXT_TYPES = {"TEXT", "MTEXT"}
SKIP_IN_POINTS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF", "DIMENSION", "HATCH"}
MAX_INSERT_DEPTH = 8
SNAP_TOLERANCE = 0.01  # m: endpoints closer than this are merged
MAX_BRIDGE_GAP = 4.0  # m: largest wall gap an opening may bridge
LAYER0_SPARSE_RATIO = 0.25  # use layer "0" lines too when wall layers hold < 25% of their length
DOUBLE_LINE_SHARE = 0.4  # 'auto': double-line when >= 40% of wall-line length pairs up
PARALLEL_SIN = math.sin(math.radians(2.0))

ROOM_WORDS = re.compile(
    r"bed|bath|kitchen|living|dining|office|reception|store|storage|hall|corridor|entrance|lobby|toilet|"
    r"\bwc\b|laundry|garage|terrace|balcon|patio|closet|call.?center|meeting|conference|study|lounge|"
    r"sal[oó]n|comedor|cocina|dormitorio|habitaci|ba[ñn]o|aseo|estar|recibidor|pasillo|terraza|"
    r"despacho|oficina|almac|vestidor|lavadero|garaje|entrada|distribuidor",
    re.IGNORECASE,
)
NOT_A_NAME = re.compile(r"^\s*(sup\.?|s\.?u\.?|area|superficie)\b|^[\d\s.,=m²2]*$", re.IGNORECASE)


class ParseError(Exception):
    """The drawing could not be turned into a building model."""


@dataclass
class ParseResult:
    building: Building
    warnings: list[str]
    stats: dict
    unresolved_furniture: list[FurnitureCandidate] = field(default_factory=list)


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
    layer0_lines: list[DXFGraphic] = field(default_factory=list)
    unclassified_lines: list[DXFGraphic] = field(default_factory=list)
    openings: list[tuple[str, DXFGraphic]] = field(default_factory=list)
    furniture: list[tuple[str | None, str, DXFGraphic]] = field(default_factory=list)  # (category, matched_by, e)
    room_shapes: list[DXFGraphic] = field(default_factory=list)
    texts: list[tuple[DXFGraphic, bool]] = field(default_factory=list)  # (entity, on a room layer)
    layer_counts: Counter = field(default_factory=Counter)


# --- Public entry point ----------------------------------------------------------------


def parse_dxf(data: bytes, options: ParseOptions, settings: Settings) -> ParseResult:
    try:
        doc = read_dxf(data)
    except DrawingReadError as exc:
        raise ParseError(str(exc)) from exc

    warnings: list[str] = []
    patterns = layer_patterns(settings, options)
    classifier = DrawingClassifier(doc, patterns)
    collected = _Collected()
    _collect(doc.modelspace(), classifier, collected, warnings)

    wall_entities = _wall_candidates(collected, patterns.wall, warnings)
    raw_segments: list[tuple[Point2, Point2]] = []
    arc_count = 0
    for entity in wall_entities:
        segs, had_arc = _segments(entity)
        raw_segments.extend(segs)
        arc_count += had_arc
    if arc_count:
        warnings.append(f"{arc_count} wall polylines contain arc segments; arcs were approximated by straight chords.")

    factor, units_label = _unit_factor(int(doc.header.get("$INSUNITS", 0) or 0), options, raw_segments, warnings)

    meters = [(scale(a, factor), scale(b, factor)) for a, b in raw_segments]
    meters = _dedupe([(a, b) for a, b in meters if math.dist(a, b) >= 0.02])
    if not meters:
        raise ParseError("All wall segments are degenerate after unit conversion. Check the drawing units.")
    origin = (min(min(a[0], b[0]) for a, b in meters), min(min(a[1], b[1]) for a, b in meters))

    def to_plan(p: Point2) -> Point2:
        return (p[0] * factor - origin[0], p[1] * factor - origin[1])

    segments = [(sub(a, origin), sub(b, origin)) for a, b in meters]
    opening_points = [(kind, [to_plan(p) for p in _entity_points(e)]) for kind, e in collected.openings]
    opening_points = [(kind, pts) for kind, pts in opening_points if pts]

    style = options.wall_style
    paired: list[_Centerline] | None = None
    if style in ("auto", "double_line"):
        pair_warnings: list[str] = []
        paired = _pair_double_lines(segments, options.min_wall_thickness, options.max_wall_thickness, pair_warnings)
        total = sum(math.dist(a, b) for a, b in segments)
        share = 2 * sum(c.length for c in paired) / total if total else 0.0
        if style == "auto":
            style = "double_line" if share >= DOUBLE_LINE_SHARE else "centerline"
        if style == "double_line":
            warnings.extend(pair_warnings)
            if not paired:
                raise ParseError(
                    "Double-line mode found no parallel wall-face pairs. Try wallStyle='centerline' or adjust "
                    "min/maxWallThickness."
                )

    thickness = options.wall_thickness or settings.default_wall_thickness
    centerlines = paired if style == "double_line" else [_Centerline(a, b, thickness) for a, b in segments]
    max_gap = options.max_wall_thickness if style == "double_line" else thickness

    _extend_to_corners(centerlines)
    centerlines = _bridge_gaps(centerlines, [pts for _, pts in opening_points], max_gap)
    _snap_endpoints(centerlines)
    centerlines = [c for c in centerlines if c.length >= MIN_WALL_LENGTH]

    ceiling = options.ceiling_height or settings.default_ceiling_height
    walls = [
        Wall(id=f"wall_{i}", start=_r(c.a), end=_r(c.b), thickness=round(c.thickness, 4), height=ceiling)
        for i, c in enumerate(centerlines, start=1)
    ]
    # Real drawings often hold other sheets/details beside the plan: ignore anything outside the walls.
    inside = _building_box(walls)
    outside = 0
    kept_openings = [(k, pts) for k, pts in opening_points if any(inside(p) for p in pts)]
    outside += len(opening_points) - len(kept_openings)
    _place_openings(walls, kept_openings, options, settings, warnings)
    rooms, dropped_rooms = _build_rooms(collected, to_plan, inside)
    outside += dropped_rooms

    candidates = []
    for category, matched_by, e in collected.furniture if options.place_furniture else []:
        points = [to_plan(p) for p in _entity_points(e)]
        if not points:
            continue
        candidate = FurnitureCandidate(
            block=e.dxf.get("name", "?"),
            layer=e.dxf.get("layer", "0"),
            category=category,
            matched_by=matched_by,
            points=points,
            round_shape=_has_circle(e),
            rotation=float(e.dxf.get("rotation", 0.0)),
        )
        if not inside(candidate.center):
            outside += 1
            continue
        if candidate.category is None and (guess := category_from_size(*candidate.local_size, candidate.layer)):
            candidate.category, candidate.matched_by = guess, "size"
        candidates.append(candidate)
    if outside:
        warnings.append(f"Ignored {outside} openings/rooms/furniture outside the building (other sheets or details).")
    resolved = [c for c in candidates if c.category]
    unresolved = [c for c in candidates if not c.category]
    furniture = place_furniture(resolved, walls, warnings)

    building = Building(ceiling_height=ceiling, walls=walls, rooms=rooms, furniture=furniture)
    for problem in validate_building(building):
        warnings.append(f"Validation: {problem.message}")

    xs = [p[0] for w in walls for p in (w.start, w.end)]
    ys = [p[1] for w in walls for p in (w.start, w.end)]
    stats = {
        "detectedUnits": units_label,
        "unitFactor": factor,
        "wallStyle": style,
        "extent": [round(max(xs, default=0), 3), round(max(ys, default=0), 3)],
        "wallCount": len(walls),
        "windowCount": sum(o.type == "window" for w in walls for o in w.openings),
        "doorCount": sum(o.type == "door" for w in walls for o in w.openings),
        "roomCount": len(rooms),
        "furnitureCount": len(furniture),
        "unresolvedFurnitureBlocks": sorted({c.block for c in unresolved}),
        "layers": dict(sorted(collected.layer_counts.items())),
    }
    return ParseResult(building=building, warnings=warnings, stats=stats, unresolved_furniture=unresolved)


# --- Entity collection -------------------------------------------------------------------


def _collect(
    entities, classifier: DrawingClassifier, out: _Collected, warnings: list[str], inherited: str | None = None,
    depth: int = 0,
) -> None:  # fmt: skip
    for entity in entities:
        layer = entity.dxf.get("layer", "0")
        if layer == "0" and inherited:
            layer = inherited
            entity.dxf.layer = inherited
        out.layer_counts[layer] += 1
        cls = classifier.classify(entity, layer)
        dxftype = entity.dxftype()

        if dxftype == "INSERT":
            if cls.category in ("window", "door"):
                out.openings.append((cls.category, entity))
            elif cls.category == "furniture":
                matched_by = "block_name" if "block name" in cls.reason else "layer"
                out.furniture.append((cls.furniture_category, matched_by, entity))
            elif depth < MAX_INSERT_DEPTH:
                try:
                    children = list(entity.virtual_entities())
                except Exception as exc:  # malformed blocks should not abort the whole parse
                    warnings.append(f"Skipped block reference {entity.dxf.get('name', '?')}: {exc}")
                    continue
                _collect(children, classifier, out, warnings, inherited=layer, depth=depth + 1)
            continue

        match cls.category:
            case "wall" if dxftype in LINEAR_TYPES:
                out.walls.append(entity)
            case "window" | "door":
                out.openings.append((cls.category, entity))
            case "room":
                out.room_shapes.append(entity)
            case "room_label":
                out.texts.append((entity, True))
            case "annotation" if dxftype in TEXT_TYPES:
                out.texts.append((entity, False))
            case "other" if dxftype in LINEAR_TYPES and depth == 0:
                out.unclassified_lines.append(entity)
                if layer == "0":
                    out.layer0_lines.append(entity)


def _wall_candidates(collected: _Collected, wall_patterns, warnings: list[str]) -> list[DXFGraphic]:
    if collected.walls:
        wall_length = sum(_length(e) for e in collected.walls)
        layer0_length = sum(_length(e) for e in collected.layer0_lines)
        if collected.layer0_lines and wall_length < LAYER0_SPARSE_RATIO * layer0_length:
            warnings.append(
                f"Wall layers hold little geometry ({wall_length:.0f} vs {layer0_length:.0f} drawing units on "
                "layer '0'); layer '0' linework was also treated as walls."
            )
            return collected.walls + collected.layer0_lines
        return collected.walls
    if not collected.unclassified_lines:
        found = ", ".join(sorted(collected.layer_counts)) or "none"
        raise ParseError(
            f"No wall geometry found. Expected lines on layers matching {list(wall_patterns)}; layers in file: {found}."
        )
    used = sorted({e.dxf.layer for e in collected.unclassified_lines})
    warnings.append(
        f"No entities on wall layers {list(wall_patterns)}; treating {len(collected.unclassified_lines)} line "
        f"entities on layers {used} as walls."
    )
    return collected.unclassified_lines


def _length(entity: DXFGraphic) -> float:
    return sum(math.dist(a, b) for a, b in _segments(entity)[0])


def _segments(entity: DXFGraphic) -> tuple[list[tuple[Point2, Point2]], bool]:
    dxftype = entity.dxftype()
    if dxftype == "LINE":
        s, e = entity.dxf.start, entity.dxf.end
        return [((s.x, s.y), (e.x, e.y))], False
    if dxftype == "POLYLINE" and (entity.is_poly_face_mesh or entity.is_polygon_mesh):
        return [], False
    points = polyline_points(entity)
    closed = entity.closed if dxftype == "LWPOLYLINE" else entity.is_closed
    if closed and len(points) > 2:
        points.append(points[0])
    return list(zip(points, points[1:], strict=False)), entity.has_arc


def _entity_points(entity: DXFGraphic) -> list[Point2]:
    parts = [e for e in recursive_decompose([entity]) if e.dxftype() not in SKIP_IN_POINTS]
    points: list[Point2] = []
    for primitive in to_primitives(parts):
        points.extend((v.x, v.y) for v in primitive.vertices())
    return points


def _has_circle(entity: DXFGraphic) -> bool:
    return any(e.dxftype() == "CIRCLE" for e in recursive_decompose([entity]))


def _unit_factor(
    insunits: int, options: ParseOptions, segments: list[tuple[Point2, Point2]], warnings: list[str]
) -> tuple[float, str]:
    if options.scale:
        return options.scale, "custom scale"
    if options.units != "auto":
        return UNIT_FACTORS[options.units], options.units

    xs = [p[0] for seg in segments for p in seg]
    ys = [p[1] for seg in segments for p in seg]
    extent = max(max(xs) - min(xs), max(ys) - min(ys)) if xs else 0.0
    lo, hi = PLAUSIBLE_EXTENT

    declared: tuple[float, str] | None = None
    if insunits:
        try:
            unit_enum = InsertUnits(insunits)
            declared = (conversion_factor(unit_enum, InsertUnits.Meters), unit_enum.name.lower())
        except (ValueError, KeyError, ZeroDivisionError):
            warnings.append(f"Unsupported $INSUNITS value {insunits}; guessing units from the building size.")
    if declared and lo <= extent * declared[0] <= hi:
        return declared

    guess = next((u for u in UNIT_PREFERENCE if lo <= extent * UNIT_FACTORS[u] <= hi), "m")
    if declared:
        warnings.append(
            f"The drawing declares {declared[1]}, which would make the building {extent * declared[0]:.3g} m "
            f"across; using '{guess}' ({extent * UNIT_FACTORS[guess]:.1f} m) instead. Pass 'units' to override."
        )
    else:
        warnings.append(
            f"Drawing has no units ($INSUNITS unset); assumed '{guess}' from its {extent:.0f}-unit extent. "
            "Pass 'units' or 'scale' to override."
        )
    return UNIT_FACTORS[guess], f"{guess} (guessed)"


def _dedupe(segments: list[tuple[Point2, Point2]]) -> list[tuple[Point2, Point2]]:
    """Drop exact duplicates (the same line drawn on two layers)."""
    seen: set[tuple] = set()
    out = []
    for a, b in segments:
        key = tuple(sorted(((round(a[0], 3), round(a[1], 3)), (round(b[0], 3), round(b[1], 3)))))
        if key not in seen:
            seen.add(key)
            out.append((a, b))
    return out


# --- Wall reconstruction -----------------------------------------------------------------


def _pair_double_lines(
    segments: list[tuple[Point2, Point2]], min_t: float, max_t: float, warnings: list[str]
) -> list[_Centerline]:
    """Pair parallel face lines into centerlines, widest gap first.

    A long face may pair with several shorter ones. Lines lying inside an accepted wall band
    (inner leaves of cavity walls) are absorbed so they cannot form extra, thinner walls.
    """
    dirs = [unit(sub(b, a)) for a, b in segments]
    neighbors: list[list[tuple[int, float]]] = [[] for _ in segments]  # (other, signed offset from i)
    candidates = []
    for i, (a, b) in enumerate(segments):
        di, li = dirs[i], math.dist(a, b)
        for j in range(i + 1, len(segments)):
            c, d = segments[j]
            if abs(cross(di, dirs[j])) > PARALLEL_SIN:
                continue
            off_c, off_d = cross(di, sub(c, a)), cross(di, sub(d, a))
            if off_c * off_d <= 0 or abs(off_c - off_d) > 0.02:
                continue
            offset = (off_c + off_d) / 2
            gap = abs(offset)
            if gap > max_t:
                continue
            tc, td = dot(sub(c, a), di), dot(sub(d, a), di)
            lo, hi = max(0.0, min(tc, td)), min(li, max(tc, td))
            if hi - lo <= 0.01:
                continue
            neighbors[i].append((j, offset))
            neighbors[j].append((i, cross(dirs[j], sub(a, c))))
            if gap >= min_t and hi - lo >= max(MIN_WALL_LENGTH, gap):
                candidates.append((-gap, -(hi - lo), i, j, lo, hi, math.copysign(1.0, offset)))

    candidates.sort()
    used: list[list[tuple[float, float]]] = [[] for _ in segments]
    centerlines: list[_Centerline] = []
    for neg_gap, _neg_overlap, i, j, lo, hi, side in candidates:
        gap = -neg_gap
        a, b = segments[i]
        di = dirs[i]
        pa, pb = add(a, scale(di, lo)), add(a, scale(di, hi))
        lo_j, hi_j = _param_range(segments[j], dirs[j], pa, pb)
        if _overlaps(used[i], lo, hi) or _overlaps(used[j], lo_j, hi_j):
            continue
        used[i].append((lo, hi))
        used[j].append((lo_j, hi_j))
        shift = scale((-di[1], di[0]), side * gap / 2)
        centerlines.append(_Centerline(add(pa, shift), add(pb, shift), gap))
        # Absorb lines strictly inside this band over the accepted interval.
        for k, offset in neighbors[i]:
            if k == j or not 0.005 < offset * side < gap - 0.005:
                continue
            c, d = segments[k]
            tk = sorted((dot(sub(c, a), di), dot(sub(d, a), di)))
            olo, ohi = max(lo, tk[0]), min(hi, tk[1])
            if ohi - olo > 0.01:
                used[k].append(_param_range(segments[k], dirs[k], add(a, scale(di, olo)), add(a, scale(di, ohi))))

    unpaired = sum(1 for (a, b), u in zip(segments, used, strict=True) if not u and math.dist(a, b) > max_t)
    if unpaired:
        warnings.append(f"{unpaired} wall lines had no parallel partner within {min_t}-{max_t} m and were ignored.")
    return centerlines


def _param_range(segment: tuple[Point2, Point2], direction: Point2, p: Point2, q: Point2) -> tuple[float, float]:
    start = segment[0]
    return tuple(sorted((dot(sub(p, start), direction), dot(sub(q, start), direction))))  # type: ignore[return-value]


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


def _bridge_gaps(
    centerlines: list[_Centerline], opening_sets: list[list[Point2]], small_gap: float
) -> list[_Centerline]:
    """Merge collinear wall pieces split by an opening, or by a small gap (e.g. at a T-junction)."""
    walls = list(centerlines)
    merged = True
    while merged:
        merged = False
        i = 0
        while i < len(walls):
            j = i + 1
            while j < len(walls):
                joined = _try_bridge(walls[i], walls[j], opening_sets, small_gap)
                if joined:
                    walls[i] = joined
                    del walls[j]
                    merged = True
                else:
                    j += 1
            i += 1
    return walls


def _try_bridge(
    w1: _Centerline, w2: _Centerline, opening_sets: list[list[Point2]], small_gap: float
) -> _Centerline | None:
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
    if t2[0] >= l1 - 0.01:
        gap_lo, gap_hi = l1, t2[0]
    elif t2[1] <= 0.01:
        gap_lo, gap_hi = t2[1], 0.0
    else:
        return None  # overlapping collinear pieces: leave as-is
    gap = gap_hi - gap_lo

    def joined() -> _Centerline:
        lo, hi = min(0.0, t2[0]), max(l1, t2[1])
        return _Centerline(add(w1.a, scale(d1, lo)), add(w1.a, scale(d1, hi)), w1.thickness)

    if gap <= small_gap + 0.01:
        return joined()
    if gap > MAX_BRIDGE_GAP:
        return None
    reach = max(w1.thickness, 0.3)
    for points in opening_sets:
        ts = [dot(sub(p, w1.a), d1) for p in points if abs(cross(d1, sub(p, w1.a))) <= reach]
        # The opening must overlap the gap (it usually spans exactly jamb to jamb).
        if ts and max(ts) > gap_lo + 0.01 and min(ts) < gap_hi - 0.01:
            return joined()
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


def _text_of(entity: DXFGraphic) -> str:
    text = entity.plain_text() if entity.dxftype() == "MTEXT" else entity.dxf.get("text", "")
    return " ".join(str(text).split())[:80]


def _building_box(walls: list[Wall], margin: float = 1.0):
    xs = [p[0] for w in walls for p in (w.start, w.end)]
    ys = [p[1] for w in walls for p in (w.start, w.end)]
    if not xs:
        return lambda _p: True
    x0, x1, y0, y1 = min(xs) - margin, max(xs) + margin, min(ys) - margin, max(ys) + margin
    return lambda p: x0 <= p[0] <= x1 and y0 <= p[1] <= y1


def _build_rooms(collected: _Collected, to_plan, inside) -> tuple[list[Room], int]:
    """Rooms inside the building, plus the number of outlines/labels dropped for lying outside it."""
    shapes = [[_r(to_plan(p)) for p in polyline_points(e)] for e in collected.room_shapes if is_closed_shape(e)]
    shapes = [s[:-1] if len(s) > 3 and s[0] == s[-1] else s for s in shapes]
    shapes = [s for s in shapes if len(s) >= 3]
    dropped = sum(not inside(polygon_centroid(s)) for s in shapes)
    shapes = [s for s in shapes if inside(polygon_centroid(s))]

    # (name, position, priority): room-layer text 0, room-like words 1, any other name-like text 2
    labels: list[tuple[str, Point2, int]] = []
    for entity, on_room_layer in collected.texts:
        text = _text_of(entity)
        if not text or NOT_A_NAME.search(text) or sum(ch.isalpha() for ch in text) < 3:
            continue
        priority = 0 if on_room_layer else 1 if ROOM_WORDS.search(text) else 2
        insert = entity.dxf.insert
        position = to_plan((insert.x, insert.y))
        if inside(position):
            labels.append((text, position, priority))

    rooms: list[Room] = []
    unused = list(labels)
    for polygon in shapes:
        inside = sorted((lab for lab in unused if point_in_polygon(lab[1], polygon)), key=lambda lab: lab[2])
        label = inside[0] if inside else None
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
    # Text-only rooms (no outlines in the drawing): keep labels that look like room names.
    for text, position, priority in unused:
        if priority <= 1:
            rooms.append(Room(id=f"room_{len(rooms) + 1}", name=text, label_position=_r(position)))
    return rooms, dropped


def _r(p: Point2) -> Point2:
    return (round(p[0], 4) + 0.0, round(p[1], 4) + 0.0)
