"""Canonical building JSON -> Babylon.js scene spec (ARCHITECTURE.md module 2).

* Every wall is the same height (the building's wall/ceiling height, 10 ft by default).
* Doors are open from the floor to the door height (7 ft); a header of the wall's thickness fills
  the space above, up to the top of the neighbouring wall.
* Windows span 2.5 ft to 6.5 ft; a sill box fills below and a header above, again matching the
  wall's thickness and top. A thin glass pane sits in the opening.
* Furniture becomes GLB placements with per-axis scaling (the library models use inconsistent
  native sizes) and rotation so each model's front faces away from its wall.

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
from app.services.geometry import point_segment_distance, sub, unit

EYE_HEIGHT = 1.6
GLASS_THICKNESS = 0.02
SLAB_THICKNESS = 0.05
MATERIALS = {
    "wall": MaterialSpec(color="#EEEAE2"),
    "header": MaterialSpec(color="#EEEAE2"),
    "sill": MaterialSpec(color="#EEEAE2"),
    "glass": MaterialSpec(color="#9EC9E8", alpha=0.35),
    "floor": MaterialSpec(color="#C9B79C"),
    "ceiling": MaterialSpec(color="#FAFAF7"),
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
    for wall in building.walls:
        wall_meshes, wall_openings = _wall_meshes(wall, building.walls)
        meshes.extend(wall_meshes)
        openings.extend(wall_openings)

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

    rooms = [
        RoomLabel(id=r.id, name=r.name, position=_v(r.label_position[0], EYE_HEIGHT, r.label_position[1]))
        for r in building.rooms
        if r.label_position
    ]
    furniture = [spec for item in building.furniture if (spec := _furniture_spec(item))]
    return BabylonScene(
        wall_height=height,
        bounds=Bounds(min=_v(x0, 0.0, z0), max=_v(x1, height, z1)),
        meshes=meshes,
        openings=openings,
        furniture=furniture,
        rooms=rooms,
        camera=_camera(building, (cx, cz)),
        materials=MATERIALS,
    )


# --- Walls ------------------------------------------------------------------------------


def _connected(point: Point2, wall: Wall, walls: list[Wall]) -> bool:
    return any(
        other is not wall and point_segment_distance(point, other.start, other.end) <= other.thickness / 2 + 0.02
        for other in walls
    )


def _wall_meshes(wall: Wall, walls: list[Wall]) -> tuple[list[BoxMesh], list[OpeningSpec]]:
    length = wall.length
    if length <= 0:
        return [], []
    dx, dz = unit(sub(wall.end, wall.start))
    rotation = _angle(-math.atan2(dz, dx))  # Babylon RotationY maps local +x to (cos a, -sin a)
    t, h = wall.thickness, wall.height
    # Extend connected ends by half a thickness so corners and T-junctions close without gaps.
    ext_start = t / 2 if _connected(wall.start, wall, walls) else 0.0
    ext_end = t / 2 if _connected(wall.end, wall, walls) else 0.0

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
    cursor = -ext_start
    index = 0
    for opening in sorted(wall.openings, key=lambda o: o.offset):
        a, b = max(opening.offset, 0.0), min(opening.offset + opening.width, length)
        if a - cursor > 1e-3:
            meshes.append(box("wall", index, cursor, a, 0.0, h, t))
            index += 1
        meshes.extend(_opening_meshes(box, opening, a, b, h, t))
        specs.append(_opening_spec(wall, opening, a, b, rotation, dx, dz))
        cursor = max(cursor, b)
    if length + ext_end - cursor > 1e-3:
        meshes.append(box("wall", index, cursor, length + ext_end, 0.0, h, t))
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
    sx, sy, sz = item.width / nx, item.height / ny, item.depth / nz
    fx, fz = item.facing
    theta = math.atan2(fx, fz)  # RotationY maps local +z to (sin a, cos a)
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
        footprint=(item.width, item.depth),
        facing=(fx, fz),
    )


# --- Camera -----------------------------------------------------------------------------


def _polygon_area(polygon: list[Point2] | None) -> float:
    if not polygon or len(polygon) < 3:
        return 0.0
    pts = list(polygon)
    return abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1], strict=True))) / 2


def _camera(building: Building, fallback: Point2) -> CameraSpec:
    """Spawn at the label of the largest room that is clear of walls (so the walkthrough starts in an
    open space, not a corridor), else the plan center. Rooms without an outline come last."""
    rooms = sorted((r for r in building.rooms if r.label_position), key=lambda r: -_polygon_area(r.polygon))
    candidates = [r.label_position for r in rooms] + [fallback]
    spot = candidates[-1]
    for point in candidates:
        clearance = min(
            (point_segment_distance(point, w.start, w.end) - w.thickness / 2 for w in building.walls), default=9.0
        )
        if clearance >= 0.5:
            spot = point
            break
    x, z = spot
    return CameraSpec(position=_v(x, EYE_HEIGHT, z), target=_v(x, EYE_HEIGHT, z + 1.0), eye_height=EYE_HEIGHT)
