"""Canonical building JSON -> Babylon.js scene spec (ARCHITECTURE.md module 2).

* Every wall is the same height (the building's wall/ceiling height, 10 ft by default).
* Doors are open from the floor to the door height (7 ft); a header of the wall's thickness fills
  the space above, up to the top of the neighbouring wall. Single doors get a hinged leaf the walker
  can open and close (services/doors.py).
* Windows span 2.5 ft to 6.5 ft; a sill box fills below and a header above, again matching the
  wall's thickness and top. A thin glass pane sits in the opening.
* Furniture becomes GLB placements with per-axis scaling (the library models use inconsistent
  native sizes) and rotation so each model's front faces away from its wall.
* The map ends a few meters around the building: open ground up to a ring of tall "boundary" walls
  that carry the landscape backdrop on their inner side, so a walker who leaves through a door sees a
  horizon and can never walk (or fall) off the world.

The spec is deterministic and regenerated after every edit; `wallId` / `openingId` on each mesh let
the client rebuild only the meshes of walls listed in an edit's `affectedIds`.
"""

import math

from app.schemas.babylon import (
    BabylonScene,
    Bounds,
    BoxMesh,
    CameraSpec,
    FurnitureSpec,
    MaterialSpec,
    OpeningSpec,
    RoomLabel,
    Vec3,
)
from app.schemas.building import Building, FurnitureItem, Opening, Point2, Wall
from app.services.furniture_catalog import CATALOG, FURNITURE_URL_PREFIX
from app.services.geometry import SegmentGrid, add, dot, point_segment_distance, scale, sub, unit

BODY_HEIGHT = 6 * 0.3048  # the walker is 6 ft tall
EYE_HEIGHT = round(BODY_HEIGHT - 0.12, 3)  # eyes ~12 cm below the top of the head: 1.709 m
GLASS_THICKNESS = 0.02
SLAB_THICKNESS = 0.05
MAP_MARGIN = 6.0  # m of open ground between the building's outline and the end of the map
BOUNDARY_HEIGHT = 10.0  # m: the backdrop walls at the end of the map
BOUNDARY_THICKNESS = 0.5
GROUND_THICKNESS = 0.2
GROUND_DROP = 0.01  # ground top sits just below the floor slab's top, so indoors the floor wins
BACKDROP_URL = "/map-end-background.jpg"  # frontend/public; shown only on the boundary walls
SKY_URL = "/sky.avif"  # frontend/public; shown only on the sky canopy over the map, never as a house ceiling
SKY_THICKNESS = 0.2
MATERIALS = {
    "wall": MaterialSpec(color="#EEEAE2"),
    "header": MaterialSpec(color="#EEEAE2"),
    "sill": MaterialSpec(color="#EEEAE2"),
    "glass": MaterialSpec(color="#9EC9E8", alpha=0.35),
    "floor": MaterialSpec(color="#C9B79C"),
    "ceiling": MaterialSpec(color="#FAFAF7"),
    "ground": MaterialSpec(color="#4A7F24"),
    "boundary": MaterialSpec(color="#2580BB", texture=BACKDROP_URL),
    "sky": MaterialSpec(color="#5B93B5", texture=SKY_URL),
}
_MODELS = {m.file: m for m in CATALOG}


def _r(value: float) -> float:
    return round(value, 4) + 0.0


def _angle(value: float) -> float:
    return round(value, 6) + 0.0


def _v(x: float, y: float, z: float) -> Vec3:
    return (_r(x), _r(y), _r(z))


def build_scene(building: Building) -> BabylonScene:
    meshes: list[BoxMesh] = []
    openings: list[OpeningSpec] = []
    for wall, span in zip(building.walls, wall_spans(building.walls), strict=True):
        wall_meshes, wall_openings = _wall_meshes(wall, span)
        meshes.extend(wall_meshes)
        openings.extend(wall_openings)
    from app.services.doors import door_leaves  # doors -> guardrails -> this module

    leaves = door_leaves(building)
    for spec in openings:
        spec.leaf = leaves.get(spec.id)

    xs = [p[0] for w in building.walls for p in (w.start, w.end)] or [0.0]
    zs = [p[1] for w in building.walls for p in (w.start, w.end)] or [0.0]
    pad = max((w.thickness for w in building.walls), default=0.0) / 2
    x0, x1, z0, z1 = min(xs) - pad, max(xs) + pad, min(zs) - pad, max(zs) + pad
    height = building.ceiling_height
    cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
    meshes.append(
        BoxMesh(
            id="floor", kind="floor", position=_v(cx, -SLAB_THICKNESS / 2, cz),
            size=_v(x1 - x0, SLAB_THICKNESS, z1 - z0), rotation_y=0.0,
        )
    )  # fmt: skip
    meshes.append(
        BoxMesh(
            id="ceiling", kind="ceiling", position=_v(cx, height + SLAB_THICKNESS / 2, cz),
            size=_v(x1 - x0, SLAB_THICKNESS, z1 - z0), rotation_y=0.0,
        )
    )  # fmt: skip
    map_bounds = Bounds(
        min=_v(x0 - MAP_MARGIN, -GROUND_DROP, z0 - MAP_MARGIN),
        max=_v(x1 + MAP_MARGIN, BOUNDARY_HEIGHT, z1 + MAP_MARGIN),
    )
    meshes.extend(_map_end_meshes(map_bounds))

    rooms = [
        RoomLabel(id=r.id, name=r.name, position=_v(r.label_position[0], EYE_HEIGHT, r.label_position[1]))
        for r in building.rooms
        if r.label_position
    ]
    furniture = [spec for item in building.furniture if (spec := _furniture_spec(item))]
    return BabylonScene(
        wall_height=height,
        bounds=Bounds(min=_v(x0, 0.0, z0), max=_v(x1, height, z1)),
        map_bounds=map_bounds,
        meshes=meshes,
        openings=openings,
        furniture=furniture,
        rooms=rooms,
        camera=_camera(building, (cx, cz)),
        materials=MATERIALS,
    )


def _map_end_meshes(bounds: Bounds) -> list[BoxMesh]:
    """Open ground up to the end of the map, closed by four backdrop walls just outside it.

    The walls are solid for the walker (nobody walks or falls off the world) and carry the landscape
    backdrop on their inner faces; a sky canopy rests on them. House walls and ceilings never use these
    materials: indoors the house's own ceiling (well below the sky) is what you see overhead.
    """
    (x0, _, z0), (x1, _, z1) = bounds.min, bounds.max
    cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
    t, h = BOUNDARY_THICKNESS, BOUNDARY_HEIGHT
    ground_y = -GROUND_DROP - GROUND_THICKNESS / 2
    return [
        BoxMesh(
            id="map:ground", kind="ground", position=_v(cx, ground_y, cz),
            size=_v(x1 - x0 + 2 * t, GROUND_THICKNESS, z1 - z0 + 2 * t), rotation_y=0.0,
        ),
        BoxMesh(id="map:boundary:south", kind="boundary", position=_v(cx, h / 2, z0 - t / 2),
                size=_v(x1 - x0 + 2 * t, h, t), rotation_y=0.0),
        BoxMesh(id="map:boundary:north", kind="boundary", position=_v(cx, h / 2, z1 + t / 2),
                size=_v(x1 - x0 + 2 * t, h, t), rotation_y=0.0),
        BoxMesh(id="map:boundary:west", kind="boundary", position=_v(x0 - t / 2, h / 2, cz),
                size=_v(z1 - z0 + 2 * t, h, t), rotation_y=_angle(math.pi / 2)),
        BoxMesh(id="map:boundary:east", kind="boundary", position=_v(x1 + t / 2, h / 2, cz),
                size=_v(z1 - z0 + 2 * t, h, t), rotation_y=_angle(math.pi / 2)),
        BoxMesh(id="map:sky", kind="sky", position=_v(cx, h + SKY_THICKNESS / 2, cz),
                size=_v(x1 - x0 + 2 * t, SKY_THICKNESS, z1 - z0 + 2 * t), rotation_y=0.0),
    ]  # fmt: skip


# --- Walls ------------------------------------------------------------------------------


MAX_JOINT_GAP = 0.45  # m: a wall end this close to another wall is joined to it (closes drafting gaps)


def _end_extension(point: Point2, outward: Point2, wall: Wall, walls: list[Wall]) -> float:
    """How far to extend a wall end (along the unit vector `outward`) so it joins the wall it meets.

    * Corner (the end meets the other wall near that wall's end): reach the other wall's far face, so
      the outer corner is filled even when the two walls have different thicknesses.
    * T-junction (the end meets the middle of the other wall): reach its centerline.
    * Collinear continuation: bridge the gap to the other wall's end.
    Drawings often stop a wall a few centimetres short of its neighbour; ends up to MAX_JOINT_GAP away
    are joined. Free ends (nothing ahead within reach) are left as drawn.
    """
    # (passes beside the other wall's end, distance ahead, extension): a wall the ray really hits wins over
    # one whose end it only passes (that wall usually meets this one's face from the side, a T-junction).
    best: tuple[bool, float, float] | None = None
    for other in walls:
        length = other.length
        if other is wall or length <= 0:
            continue
        direction = unit(sub(other.end, other.start))
        normal = (-direction[1], direction[0])
        cos = dot(outward, normal)
        half = other.thickness / 2
        if abs(cos) < 0.2:  # (nearly) parallel: only a continuation counts, possibly with a small jog
            if abs(dot(sub(point, other.start), normal)) > (wall.thickness + other.thickness) / 2 + 0.01:
                continue  # side by side, not in line: their thickness bands don't overlap
            for q in (other.start, other.end):
                ahead = dot(sub(q, point), outward)
                if 0.0 <= ahead <= MAX_JOINT_GAP and (best is None or (False, ahead) < best[:2]):
                    best = (False, ahead, ahead)
            continue
        ahead = dot(sub(other.start, point), normal) / cos  # along `outward` to the other centerline
        if ahead < -half / abs(cos) - 0.01 or ahead > MAX_JOINT_GAP + half / abs(cos):
            continue  # the other wall is behind this end, or too far ahead
        along = dot(sub(add(point, scale(outward, ahead)), other.start), direction)
        margin = wall.thickness / 2 + 0.05
        if along < -MAX_JOINT_GAP or along > length + MAX_JOINT_GAP:
            continue  # the ray passes beside the other wall (whose own end may stop short of this corner)
        at_corner = along < margin or along > length - margin
        extension = ahead + (half / abs(cos) if at_corner else 0.0)
        beside = along < 0.0 or along > length
        if best is None or (beside, ahead) < best[:2]:
            best = (beside, ahead, extension)
    return max(0.0, best[2]) if best else 0.0


def wall_spans(walls: list[Wall]) -> list[tuple[float, float]]:
    """Rendered extent of each wall along its own axis, from its start point: (-start extension,
    length + end extension). Ends reach into the walls they meet (see `_end_extension`), so corners,
    T-junctions, and small drafting gaps close. A spatial grid limits each end to nearby walls."""
    if not walls:
        return []
    reach = 2 * MAX_JOINT_GAP + 5 * max(w.thickness for w in walls) / 2 + 0.05  # farthest wall an end can join
    grid = SegmentGrid([(w.start, w.end) for w in walls], cell=max(1.0, reach))
    spans: list[tuple[float, float]] = []
    for index, wall in enumerate(walls):
        length = wall.length
        if length <= 0:
            spans.append((0.0, 0.0))
            continue
        dx, dz = unit(sub(wall.end, wall.start))
        extensions = []
        for point, outward in ((wall.start, (-dx, -dz)), (wall.end, (dx, dz))):
            near = [walls[j] for j in grid.near(*point, *point, margin=reach) if j != index]
            extensions.append(_end_extension(point, outward, wall, near))
        spans.append((-extensions[0], length + extensions[1]))
    return spans


def _wall_meshes(wall: Wall, span: tuple[float, float]) -> tuple[list[BoxMesh], list[OpeningSpec]]:
    length = wall.length
    if length <= 0:
        return [], []
    dx, dz = unit(sub(wall.end, wall.start))
    rotation = _angle(-math.atan2(dz, dx))  # Babylon RotationY maps local +x to (cos a, -sin a)
    t, h = wall.thickness, wall.height

    def box(kind: str, index: int, s0: float, s1: float, y0: float, y1: float, depth: float, opening=None) -> BoxMesh:
        mid = (s0 + s1) / 2
        x, z = wall.start[0] + dx * mid, wall.start[1] + dz * mid
        return BoxMesh(
            id=f"{wall.id}:{kind}:{index}", kind=kind, wall_id=wall.id,
            opening_id=opening.id if opening else None,
            position=_v(x, (y0 + y1) / 2, z), size=_v(s1 - s0, y1 - y0, depth), rotation_y=rotation,
        )  # fmt: skip

    meshes: list[BoxMesh] = []
    specs: list[OpeningSpec] = []
    cursor, wall_end = span
    index = 0
    for opening in sorted(wall.openings, key=lambda o: o.offset):
        a, b = max(opening.offset, 0.0), min(opening.offset + opening.width, length)
        if a - cursor > 1e-3:
            meshes.append(box("wall", index, cursor, a, 0.0, h, t))
            index += 1
        meshes.extend(_opening_meshes(box, opening, a, b, h, t))
        specs.append(_opening_spec(wall, opening, a, b, rotation, dx, dz))
        cursor = max(cursor, b)
    if wall_end - cursor > 1e-3:
        meshes.append(box("wall", index, cursor, wall_end, 0.0, h, t))
    return meshes, specs


def _opening_meshes(box, opening: Opening, a: float, b: float, h: float, t: float) -> list[BoxMesh]:
    top = min(opening.sill_height + opening.height, h)
    meshes = []
    if h - top > 1e-3:
        meshes.append(box("header", 0, a, b, top, h, t, opening))
    if opening.type == "window":
        if opening.sill_height > 1e-3:
            meshes.append(box("sill", 0, a, b, 0.0, opening.sill_height, t, opening))
        meshes.append(box("glass", 0, a, b, opening.sill_height, top, GLASS_THICKNESS, opening))
    for mesh in meshes:  # make ids unique per opening
        mesh.id = f"{mesh.wall_id}:{mesh.kind}:{opening.id}"
    return meshes


def _opening_spec(wall: Wall, opening: Opening, a: float, b: float, rotation: float, dx: float, dz: float):
    mid = (a + b) / 2
    return OpeningSpec(
        id=opening.id,
        type=opening.type,
        wall_id=wall.id,
        position=_v(wall.start[0] + dx * mid, opening.sill_height + opening.height / 2, wall.start[1] + dz * mid),
        width=_r(b - a),
        height=opening.height,
        sill_height=opening.sill_height,
        rotation_y=rotation,
    )


# --- Furniture --------------------------------------------------------------------------


def _furniture_spec(item: FurnitureItem) -> FurnitureSpec | None:
    model = _MODELS.get(item.model)
    if model is None:
        return None
    nx, ny, nz = model.native_size
    width, depth = rendered_footprint(item)
    sideways = model.front in ("+x", "-x")  # the model's width runs along its local z
    sx, sy, sz = (depth / nx, item.height / ny, width / nz) if sideways else (width / nx, item.height / ny, depth / nz)
    fx, fz = item.facing
    # RotationY maps local +z to (sin a, cos a); turn the model's own front (FRONT_ANGLE) onto `facing`.
    theta = math.atan2(fx, fz) - FRONT_ANGLE[model.front]
    # Babylon's glTF loader mirrors x (__root__: scale z -1, rotate 180 deg), so the native center's
    # x flips. Offset the parent node so the scaled model's footprint center lands on item.position
    # and its bottom on the floor.
    cx, cy, cz = model.native_center
    vx, vz = -cx * sx, cz * sz
    rx, rz = vx * math.cos(theta) + vz * math.sin(theta), -vx * math.sin(theta) + vz * math.cos(theta)
    bottom = (cy - ny / 2) * sy
    px, pz = item.position
    return FurnitureSpec(
        id=item.id,
        category=item.category,
        model=item.model,
        url=FURNITURE_URL_PREFIX + item.model,
        position=_v(px - rx, -bottom, pz - rz),
        rotation_y=_angle(theta),
        scaling=_v(sx, sy, sz),
        footprint_center=_v(px, 0.0, pz),
        footprint=(round(width, 4), round(depth, 4)),
        facing=(fx, fz),
    )


FRONT_ANGLE = {"+z": 0.0, "+x": math.pi / 2, "-z": math.pi, "-x": -math.pi / 2}  # local direction -> RotationY angle
MAX_STRETCH = 1.5  # a model's width/depth ratio may deviate from its own by at most MAX_STRETCH**2
SIZE_RANGE = (0.5, 1.6)  # rendered footprint vs the model's real-world size


def rendered_footprint(item: FurnitureItem) -> tuple[float, float]:
    """(width, depth) the model is drawn with: the drawing's footprint, never larger than it.

    The model fills the outline drawn in the plan exactly when the outline's proportions are within
    MAX_STRETCH**2 of the model's own. When they are not (block outlines that include chairs around a
    table, or odd drafting), the model keeps believable proportions and fills the outline in one
    direction, centered, without poking out of it in the other. Outlines far larger than the real piece
    are capped at SIZE_RANGE[1] x its real size; tiny ones are raised to SIZE_RANGE[0] x (the
    guardrails then keep such items clear of walls and other furniture).

    Idempotent: a footprint this function returned is returned unchanged, so guardrails can store
    rendered sizes in the model.
    """
    model = _MODELS.get(item.model)
    if model is None:
        return item.width, item.depth
    real_w, _, real_d = model.real_size
    own = real_w / real_d
    ratio = min(max(item.width / item.depth, own / MAX_STRETCH**2), own * MAX_STRETCH**2)
    width = min(item.width, item.depth * ratio)
    depth = width / ratio
    size = math.sqrt((width / real_w) * (depth / real_d))
    k = min(max(size, SIZE_RANGE[0]), SIZE_RANGE[1]) / size
    return width * k, depth * k


# --- Camera -----------------------------------------------------------------------------


MIN_SPAWN_CLEARANCE = 0.5  # m between the walker and the nearest wall or furniture


def _clearance(point: Point2, building: Building) -> float:
    """Free distance around a point: nearest wall face or furniture footprint (approximated by its circumradius)."""
    walls = (point_segment_distance(point, w.start, w.end) - w.thickness / 2 for w in building.walls)
    furniture = (math.dist(point, f.position) - math.hypot(f.width, f.depth) / 2 for f in building.furniture)
    return min([*walls, *furniture], default=9.0)


def _camera(building: Building, fallback: Point2) -> CameraSpec:
    """Spawn at the room label with the most open space around it (the main room, not a corridor or a
    store), else the plan center. Real drawings rarely have room outlines, so clearance is the measure."""
    candidates = [r.label_position for r in building.rooms if r.label_position]
    scored = [
        (clearance, point) for point in candidates if (clearance := _clearance(point, building)) >= MIN_SPAWN_CLEARANCE
    ]
    x, z = max(scored)[1] if scored else fallback
    return CameraSpec(
        position=_v(x, EYE_HEIGHT, z), target=_v(x, EYE_HEIGHT, z + 1.0), eye_height=EYE_HEIGHT, body_height=BODY_HEIGHT
    )
