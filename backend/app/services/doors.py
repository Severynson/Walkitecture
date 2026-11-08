"""Hinged door leaves for single doors (the scene spec's `openings[].leaf`).

A single door (up to SINGLE_DOOR_MAX wide) gets the library's door leaf, scaled to the opening and
hinged at one of its edges. Drawings rarely say reliably which edge carries the hinge or which way the
door swings, so every combination (hinge at either edge x opening to either side) is tried, and the
one whose swing sweeps through nothing (furniture, other walls) wins; ties go to a hinge next to a
corner, the usual place for it. The frontend opens and closes the leaf (E) by adding `swing` to the
pivot's rotation, and its collider moves with it.

Doors never block each other or a hallway: an open leaf must not cut the walking path (0.9 m, as in
`guardrails.make_walkable`) between any two doors of the room it opens into, so it never stands across a
hallway or in front of another doorway; doors close enough for their swings to meet choose together, so
that no two leaves sweep the same floor. A door that cannot open either way without blocking, or that
cannot avoid another leaf (the narrower of the two), gets no leaf: an open doorway, nothing to bump into.
"""

import math
from dataclasses import dataclass
from itertools import product

from app.schemas.babylon import DoorLeafSpec, Vec3
from app.schemas.building import Building, Opening, Point2, Wall
from app.services import guardrails
from app.services.furniture_catalog import DOOR_LEAF, FURNITURE_URL_PREFIX
from app.services.geometry import add, dot, scale, sub, unit
from app.services.guardrails import Box, furniture_box, penetration, wall_boxes

SINGLE_DOOR_MAX = 1.2  # m: wider openings are double doors or open passages (no leaf)
MIN_LEAF_WIDTH = 0.45
LEAF_THICKNESS = 0.045
OPEN_ANGLE = math.radians(88)  # stops just short of the wall it opens towards
SWEEP_STEPS = 6
SWEEP_TOLERANCE = 0.03  # m: brushing a wall at the hinge corner is fine
ARC_POINTS = 9  # outline of the floor a leaf sweeps: the hinge plus this many points on its arc
CLASH_TOLERANCE = 0.02  # m: two sweeps touching at a shared corner do not block each other
EXHAUSTIVE_DOORS = 6  # doors that may meet: every combination up to this many (4 ** 6), else one by one


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


@dataclass(frozen=True)
class _Swing:
    """One way to hang a door: the hinge, the leaf's direction when closed, the side it opens to."""

    # (doorways cut off while it stands open, obstacle hits, hinge-to-corner distance, side, hinge at the end)
    key: tuple[int, int, float, float, bool]
    hinge: Point2
    closed: Point2
    towards: Point2
    sweep: tuple[Point2, ...]  # convex outline of the floor the leaf sweeps


def _open_leaf(hinge: Point2, closed: Point2, towards: Point2, width: float) -> Box:
    u = unit(add(scale(closed, math.cos(OPEN_ANGLE)), scale(towards, math.sin(OPEN_ANGLE))))
    center = add(hinge, scale(u, width / 2))
    return Box(center[0], center[1], u[0], u[1], width / 2, LEAF_THICKNESS / 2)


class _Passages:
    """Which doors of each room the walker can reach from which (0.9 m paths past walls and furniture, as in
    `guardrails.make_walkable`), and which of those connections an open leaf would cut: a leaf standing across a
    hallway, or in front of another doorway."""

    def __init__(self, building: Building):
        self.floor = guardrails._floor(building)
        self.slots = {name.removeprefix("doorway "): box for name, box in guardrails.door_clearances(building.walls)}
        self._before: dict[int, dict[str, int]] = {}

    def _cells(self, box: Box, grow: float) -> set[int]:
        regions = self.floor.regions
        x0, y0, cell, nx, ny = regions.x0, regions.y0, regions.cell, regions.nx, regions.ny
        bx0, by0, bx1, by1 = box.bbox()
        out = set()
        for j in range(
            max(0, math.floor((by0 - grow - y0) / cell)), min(ny - 1, math.floor((by1 + grow - y0) / cell)) + 1
        ):
            py = y0 + (j + 0.5) * cell - box.cy
            for i in range(
                max(0, math.floor((bx0 - grow - x0) / cell)), min(nx - 1, math.floor((bx1 + grow - x0) / cell)) + 1
            ):
                px = x0 + (i + 0.5) * cell - box.cx
                du = max(abs(px * box.ux + py * box.uy) - box.hu, 0.0)
                dv = max(abs(py * box.ux - px * box.uy) - box.hv, 0.0)
                if du * du + dv * dv <= grow * grow:
                    out.add(j * nx + i)
        return out

    def _components(self, label: int, blocked: set[int]) -> dict[str, int]:
        """Door id -> which part of room `label` it opens into, with `blocked` cells taken away."""
        floor = self.floor
        regions, n = floor.regions, floor.regions.nx * floor.regions.ny
        part: dict[int, int] = {}
        out: dict[str, int] = {}
        for door, start in floor.entries[label]:
            if start in blocked:
                out[door] = -1 - len(out)  # its own part: cut off
                continue
            if start not in part:
                mark, stack = len(out), [start]
                part[start] = mark
                while stack:
                    idx = stack.pop()
                    for nb in guardrails._neighbors(idx, regions.nx, n):
                        if nb in part or nb in blocked or regions.labels[nb] != label:
                            continue
                        if floor.solid[nb] or floor.tight[nb] or floor.furniture[nb]:
                            continue
                        part[nb] = mark
                        stack.append(nb)
            out[door] = part[start]
        return out

    def blocks(self, opening: Opening, hinge: Point2, closed: Point2, towards: Point2, mid: Point2) -> int:
        """How many connections between the doors of the room this leaf opens into it cuts while open."""
        floor = self.floor
        if floor is None:
            return 0
        regions = floor.regions
        label = next(
            (regions.labels[idx] for door, idx in (e for es in floor.entries.values() for e in es)
             if door == opening.id and dot(sub(guardrails._cell_center(regions, idx), mid), towards) > 0),
            None,
        )  # fmt: skip
        if label is None or len(floor.entries[label]) < 2:
            return 0
        leaf = _open_leaf(hinge, closed, towards, opening.width)
        own = self._cells(self.slots[opening.id], 0.0) if opening.id in self.slots else set()
        blocked = self._cells(leaf, regions.cell / 2) | (self._cells(leaf, guardrails.PATH_WIDTH / 2) - own)
        if label not in self._before:
            self._before[label] = self._components(label, set())
        before, after = self._before[label], self._components(label, blocked)
        doors = sorted(before)
        return sum(
            1 for i, a in enumerate(doors) for b in doors[i + 1 :] if before[a] == before[b] and after[a] != after[b]
        )


def _swings(wall: Wall, opening: Opening, obstacles: list[Box], passages: _Passages | None = None) -> list[_Swing]:
    d = unit(sub(wall.end, wall.start))
    n = _left(d)
    width = opening.width
    mid = add(wall.start, scale(d, opening.offset + width / 2))
    out = []
    for at_start in (True, False):
        hinge = add(wall.start, scale(d, opening.offset if at_start else opening.offset + width))
        closed = d if at_start else scale(d, -1.0)
        corner = opening.offset if at_start else wall.length - opening.offset - width  # hinge-side jamb to wall end
        for side in (1.0, -1.0):
            towards = scale(n, side)
            hits = _sweep_hits(hinge, closed, towards, width, obstacles)
            blocks = passages.blocks(opening, hinge, closed, towards, mid) if passages else 0
            arc = tuple(
                add(hinge, scale(add(scale(closed, math.cos(a)), scale(towards, math.sin(a))), width))
                for a in (OPEN_ANGLE * k / (ARC_POINTS - 1) for k in range(ARC_POINTS))
            )
            key = (blocks, hits, round(corner, 3), -side, not at_start)
            out.append(_Swing(key, hinge, closed, towards, (hinge, *arc)))
    return out


def _overlap(a: tuple[Point2, ...], b: tuple[Point2, ...]) -> float:
    """How deep two convex outlines overlap (<= 0: apart), by the separating axis theorem."""
    depth = math.inf
    for poly in (a, b):
        for i, p in enumerate(poly):
            edge = sub(poly[(i + 1) % len(poly)], p)
            if math.hypot(*edge) < 1e-9:
                continue
            axis = unit(_left(edge))
            pa, pb = [dot(q, axis) for q in a], [dot(q, axis) for q in b]
            depth = min(depth, min(max(pa), max(pb)) - max(min(pa), min(pb)))
            if depth <= 0:
                return depth
    return depth


@dataclass
class _Door:
    wall: Wall
    opening: Opening
    mid: Point2
    swings: list[_Swing]


def _clash_table(doors: list[_Door]) -> set[tuple[int, int, int, int]]:
    """(door i, its swing, door j > i, its swing) for every pair of swings that sweep the same floor."""
    out = set()
    for i in range(len(doors)):
        for j in range(i + 1, len(doors)):
            for (oi, a), (oj, b) in product(enumerate(doors[i].swings), enumerate(doors[j].swings)):
                if _overlap(a.sweep, b.sweep) > CLASH_TOLERANCE:
                    out.add((i, oi, j, oj))
    return out


def _clashing(clashes: set, choice: tuple[int, ...]) -> list[tuple[int, int]]:
    return [
        (i, j) for i in range(len(choice)) for j in range(i + 1, len(choice)) if (i, choice[i], j, choice[j]) in clashes
    ]


def _cost(doors: list[_Door], clashes: set, choice: tuple[int, ...]) -> tuple:
    keys = [doors[i].swings[c].key for i, c in enumerate(choice)]
    return (len(_clashing(clashes, choice)), *(sum(k[f] for k in keys) for f in range(len(keys[0]))))


def _best(doors: list[_Door], clashes: set) -> tuple[int, ...]:
    """The swing per door with the fewest clashes, then the fewest obstacle hits, then the usual hinge side."""
    if len(doors) <= EXHAUSTIVE_DOORS:
        return min(product(range(4), repeat=len(doors)), key=lambda c: _cost(doors, clashes, c))
    choice: tuple[int, ...] = ()
    for door in doors:  # too many to try every combination: one at a time, in order
        choice += (min(range(len(door.swings)), key=lambda o: _cost(doors, clashes, (*choice, o))),)  # noqa: B023
    return choice


def _settle_group(doors: list[_Door]) -> dict[str, _Swing]:
    """Swings for doors whose sweeps may meet; a door that cannot avoid the others gets none."""
    doors = list(doors)
    while True:
        clashes = _clash_table(doors)
        choice = _best(doors, clashes)
        clash = _clashing(clashes, choice)
        if not clash:
            return {door.opening.id: door.swings[c] for door, c in zip(doors, choice, strict=True)}
        a, b = doors[clash[0][0]], doors[clash[0][1]]
        doors.remove(a if a.opening.width < b.opening.width else b)  # the narrower one stays an open doorway


def _groups(doors: list[_Door]) -> list[list[_Door]]:
    """Doors close enough for their swings to meet, together (union-find on the reach of each leaf)."""
    parent = list(range(len(doors)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(doors)):
        for j in range(i + 1, len(doors)):
            # a leaf sweeps up to its width from the hinge, which is half a width from the doorway's middle
            if math.dist(doors[i].mid, doors[j].mid) <= 1.5 * (doors[i].opening.width + doors[j].opening.width):
                parent[find(i)] = find(j)
    out: dict[int, list[_Door]] = {}
    for i, door in enumerate(doors):
        out.setdefault(find(i), []).append(door)
    return list(out.values())


def _spec(opening: Opening, swing: _Swing) -> DoorLeafSpec:
    hinge, closed, towards = swing.hinge, swing.closed, swing.towards
    # Pivot rotation: RotationY(a) maps local +x to (cos a, -sin a); +pi/2 swings to the right of `closed`.
    rotation = -math.atan2(closed[1], closed[0])
    swing_angle = -OPEN_ANGLE if towards == _left(closed) else OPEN_ANGLE

    # The model's width runs along its z (hinge edge at -z): turn it +90 deg so z -> pivot +x, then move its
    # hinge edge, bottom, and thickness center onto the pivot. Loaded by Babylon, glTF x is mirrored.
    width = opening.width
    (_, ny, nz), (cx, cy, cz) = DOOR_LEAF.native_size, DOOR_LEAF.native_center
    sz, sy = width / nz, opening.height / ny
    sx = sz  # keep the leaf's thickness in proportion
    hinge_z, bottom_y, middle_x = cz - nz / 2, cy - ny / 2, -cx
    return DoorLeafSpec(
        url=FURNITURE_URL_PREFIX + DOOR_LEAF.file,
        hinge=_v(hinge[0], opening.sill_height, hinge[1]),
        rotation_y=_r(rotation),
        swing=_r(swing_angle),
        model_position=_v(-sz * hinge_z, -sy * bottom_y, sx * middle_x),
        model_rotation_y=_r(math.pi / 2),
        scaling=_v(sx, sy, sz),
        size=_v(width, opening.height, LEAF_THICKNESS),
    )


def door_leaves(building: Building) -> dict[str, DoorLeafSpec]:
    """Opening id -> leaf, for every single door of the building that can swing without blocking another."""
    walls = wall_boxes(building.walls)
    furniture = [furniture_box(item) for item in building.furniture]
    passages = _Passages(building)
    doors: list[_Door] = []
    for wall in building.walls:
        openings = [o for o in wall.openings if o.type == "door" and MIN_LEAF_WIDTH <= o.width <= SINGLE_DOOR_MAX]
        if not openings:
            continue
        others = furniture + [box for other, box in walls if other.id != wall.id]
        d = unit(sub(wall.end, wall.start))
        for opening in openings:
            mid = add(wall.start, scale(d, opening.offset + opening.width / 2))
            reach = opening.width * 1.5 + 0.1  # the swing stays within this of the opening's middle
            near = [
                box for box in others
                if (b := box.bbox())[0] <= mid[0] + reach and b[2] >= mid[0] - reach
                and b[1] <= mid[1] + reach and b[3] >= mid[1] - reach
            ]  # fmt: skip
            swings = _swings(wall, opening, near, passages)
            if all(swing.key[0] for swing in swings):
                continue  # open either way, it would stand across a hallway or a doorway: an open doorway instead
            doors.append(_Door(wall, opening, mid, swings))
    by_id = {door.opening.id: door for door in doors}
    leaves: dict[str, DoorLeafSpec] = {}
    for group in _groups(doors):
        for opening_id, swing in _settle_group(group).items():
            leaves[opening_id] = _spec(by_id[opening_id].opening, swing)
    return leaves
