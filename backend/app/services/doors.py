"""Hinged door leaves for single doors (the scene spec's `openings[].leaf`).

A single door (up to SINGLE_DOOR_MAX wide) gets the library's door leaf, scaled to the opening and
hinged at one of its edges. Drawings rarely say reliably which edge carries the hinge or which way the
door swings, so every combination (hinge at either edge x opening to either side) is tried, and the
one whose swing sweeps through nothing (furniture, other walls) wins; ties go to a hinge next to a
corner, the usual place for it. The frontend opens and closes the leaf (E) by adding `swing` to the
pivot's rotation, and its collider moves with it.
"""

import math

from app.schemas.babylon import DoorLeafSpec, Vec3
from app.schemas.building import Building, Opening, Point2, Wall
from app.services.furniture_catalog import DOOR_LEAF, FURNITURE_URL_PREFIX
from app.services.geometry import add, scale, sub, unit
from app.services.guardrails import Box, furniture_box, penetration, wall_boxes

SINGLE_DOOR_MAX = 1.2  # m: wider openings are double doors or open passages (no leaf)
MIN_LEAF_WIDTH = 0.45
LEAF_THICKNESS = 0.045
OPEN_ANGLE = math.radians(88)  # stops just short of the wall it opens towards
SWEEP_STEPS = 6
SWEEP_TOLERANCE = 0.03  # m: brushing a wall at the hinge corner is fine


def _r(value: float) -> float:
    return round(value, 4) + 0.0


def _v(x: float, y: float, z: float) -> Vec3:
    return (_r(x), _r(y), _r(z))


def _left(v: Point2) -> Point2:
    return (-v[1], v[0])


def _sweep_hits(hinge: Point2, closed: Point2, towards: Point2, width: float, obstacles: list[Box]) -> int:
    """How many of the leaf's positions while opening cut into an obstacle."""
    hits = 0
    for step in range(1, SWEEP_STEPS + 1):
        angle = OPEN_ANGLE * step / SWEEP_STEPS
        u = unit(add(scale(closed, math.cos(angle)), scale(towards, math.sin(angle))))
        center = add(hinge, scale(u, width / 2))
        leaf = Box(center[0], center[1], u[0], u[1], width / 2, LEAF_THICKNESS / 2)
        hits += sum(1 for box in obstacles if penetration(leaf, box)[0] > SWEEP_TOLERANCE)
    return hits


def _leaf(wall: Wall, opening: Opening, obstacles: list[Box]) -> DoorLeafSpec:
    d = unit(sub(wall.end, wall.start))
    n = _left(d)
    width = opening.width
    options = []
    for at_start in (True, False):
        hinge = add(wall.start, scale(d, opening.offset if at_start else opening.offset + width))
        closed = d if at_start else scale(d, -1.0)
        corner = opening.offset if at_start else wall.length - opening.offset - width  # hinge-side jamb to wall end
        for side in (1.0, -1.0):
            towards = scale(n, side)
            hits = _sweep_hits(hinge, closed, towards, width, obstacles)
            options.append(((hits, round(corner, 3), -side, not at_start), hinge, closed, towards))
    _, hinge, closed, towards = min(options, key=lambda o: o[0])

    # Pivot rotation: RotationY(a) maps local +x to (cos a, -sin a); +pi/2 swings to the right of `closed`.
    rotation = -math.atan2(closed[1], closed[0])
    swing = -OPEN_ANGLE if towards == _left(closed) else OPEN_ANGLE

    # The model's width runs along its z (hinge edge at -z): turn it +90 deg so z -> pivot +x, then move its
    # hinge edge, bottom, and thickness center onto the pivot. Loaded by Babylon, glTF x is mirrored.
    (_, ny, nz), (cx, cy, cz) = DOOR_LEAF.native_size, DOOR_LEAF.native_center
    sz, sy = width / nz, opening.height / ny
    sx = sz  # keep the leaf's thickness in proportion
    hinge_z, bottom_y, middle_x = cz - nz / 2, cy - ny / 2, -cx
    return DoorLeafSpec(
        url=FURNITURE_URL_PREFIX + DOOR_LEAF.file,
        hinge=_v(hinge[0], opening.sill_height, hinge[1]),
        rotation_y=_r(rotation),
        swing=_r(swing),
        model_position=_v(-sz * hinge_z, -sy * bottom_y, sx * middle_x),
        model_rotation_y=_r(math.pi / 2),
        scaling=_v(sx, sy, sz),
        size=_v(width, opening.height, LEAF_THICKNESS),
    )


def door_leaves(building: Building) -> dict[str, DoorLeafSpec]:
    """Opening id -> leaf, for every single door of the building."""
    walls = wall_boxes(building.walls)
    furniture = [furniture_box(item) for item in building.furniture]
    leaves: dict[str, DoorLeafSpec] = {}
    for wall in building.walls:
        doors = [o for o in wall.openings if o.type == "door" and MIN_LEAF_WIDTH <= o.width <= SINGLE_DOOR_MAX]
        if not doors:
            continue
        others = furniture + [box for other, box in walls if other.id != wall.id]
        d = unit(sub(wall.end, wall.start))
        for opening in doors:
            mid = add(wall.start, scale(d, opening.offset + opening.width / 2))
            reach = opening.width * 1.5 + 0.1  # the swing stays within this of the opening's middle
            near = [
                box for box in others
                if (b := box.bbox())[0] <= mid[0] + reach and b[2] >= mid[0] - reach
                and b[1] <= mid[1] + reach and b[3] >= mid[1] - reach
            ]  # fmt: skip
            leaves[opening.id] = _leaf(wall, opening, near)
    return leaves
