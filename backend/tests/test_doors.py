"""Hinged door leaves for single doors (services/doors.py)."""

import math

import pytest

from app.schemas.building import Building, FurnitureItem, Opening, Wall
from app.services import doors
from app.services.babylon_scene import build_scene
from app.services.doors import OPEN_ANGLE
from app.services.furniture_catalog import DOOR_LEAF

H = 3.048


def room(door_width=0.9, furniture=()):
    """4 x 4 m room; the south wall (0,0)->(4,0) has a door 1.0 m from its start."""
    door = Opening(id="door_1", type="door", offset=1.0, width=door_width, height=2.134)
    walls = [
        Wall(id="s", start=(0, 0), end=(4, 0), thickness=0.2, height=H, openings=[door]),
        Wall(id="e", start=(4, 0), end=(4, 4), thickness=0.2, height=H),
        Wall(id="n", start=(4, 4), end=(0, 4), thickness=0.2, height=H),
        Wall(id="w", start=(0, 4), end=(0, 0), thickness=0.2, height=H),
    ]
    return Building(ceiling_height=H, walls=walls, furniture=list(furniture))


def leaf_of(building):
    (opening,) = build_scene(building).openings
    return opening.leaf


def leaf_corners(leaf, opened=False):
    """World x/z of the model's hinge edge and latch edge (mid-thickness, floor), as Babylon would place them."""
    (_, _, nz), (cx, _, cz) = DOOR_LEAF.native_size, DOOR_LEAF.native_center
    sx, _, sz = leaf.scaling
    out = []
    for z in (cz - nz / 2, cz + nz / 2):
        # model space (glTF x mirrored by the loader) -> scaled -> model rotation -> pivot -> world
        lx, lz = -cx * sx, z * sz
        a = leaf.model_rotation_y
        px = lx * math.cos(a) + lz * math.sin(a) + leaf.model_position[0]
        pz = -lx * math.sin(a) + lz * math.cos(a) + leaf.model_position[2]
        b = leaf.rotation_y + (leaf.swing if opened else 0.0)
        out.append(
            (leaf.hinge[0] + px * math.cos(b) + pz * math.sin(b), leaf.hinge[2] - px * math.sin(b) + pz * math.cos(b))
        )
    return out


def test_a_closed_leaf_fills_its_doorway():
    leaf = leaf_of(room())
    hinge, latch = leaf_corners(leaf)
    assert sorted([hinge[0], latch[0]]) == pytest.approx([1.0, 1.9], abs=1e-3)
    assert hinge[1] == pytest.approx(0.0, abs=1e-3) and latch[1] == pytest.approx(0.0, abs=1e-3)
    assert leaf.size == pytest.approx((0.9, 2.134, 0.045))
    assert abs(leaf.swing) == pytest.approx(OPEN_ANGLE, abs=1e-4)


def test_an_open_leaf_stands_across_the_wall_on_its_swing_side():
    leaf = leaf_of(room())
    hinge, latch = leaf_corners(leaf, opened=True)
    assert latch[0] == pytest.approx(hinge[0], abs=0.05)  # perpendicular to the wall (88 deg)
    assert abs(latch[1] - hinge[1]) == pytest.approx(0.9 * math.sin(OPEN_ANGLE), abs=1e-3)


def test_the_hinge_goes_next_to_the_corner_and_the_door_swings_away_from_furniture():
    leaf = leaf_of(room())
    assert leaf.hinge[0] == pytest.approx(1.0)  # 1.0 m from the corner, vs 2.1 m for the other jamb
    inside = leaf_corners(leaf, opened=True)[1][1] > 0
    assert inside  # by default it opens into the room (the wall's left side)
    closet = FurnitureItem(id="f1", category="closet", model="closet_by_quaternius.glb", position=(1.5, 0.45),
                           facing=(0.0, 1.0), width=1.2, depth=0.6, height=2.1)  # fmt: skip
    leaf = leaf_of(room(furniture=[closet]))
    assert leaf_corners(leaf, opened=True)[1][1] < 0  # the closet is in the way inside: it opens outwards


def test_wide_openings_have_no_leaf():
    assert leaf_of(room(door_width=1.6)) is None


def sweep(leaf):
    """The floor a leaf sweeps (plan coordinates), from its scene spec."""
    hinge, width = (leaf.hinge[0], leaf.hinge[2]), leaf.size[0]
    closed = -leaf.rotation_y  # pivot rotation -> plan angle of the closed leaf
    angles = [closed - leaf.swing * k / 8 for k in range(9)]
    return (hinge, *((hinge[0] + width * math.cos(a), hinge[1] + width * math.sin(a)) for a in angles))


def test_doors_at_a_corner_never_swing_into_each_other():
    south = Opening(id="door_1", type="door", offset=0.2, width=0.9, height=2.134)  # x 0.2-1.1
    west = Opening(id="door_2", type="door", offset=2.9, width=0.9, height=2.134)  # y 1.1-0.2
    building = room()
    building.walls[0].openings = [south]
    building.walls[3].openings = [west]
    leaves = {o.id: o.leaf for o in build_scene(building).openings}
    # Alone, each would hinge by the corner and open into the room: the two leaves would collide.
    assert leaves["door_1"] and leaves["door_2"]
    assert doors._overlap(sweep(leaves["door_1"]), sweep(leaves["door_2"])) <= doors.CLASH_TOLERANCE


def test_a_door_never_opens_across_a_hallway():
    # 6 x 4 m: hallway along the south (y 0.1-1.2) with a door at each end; door_c leads from it to the room
    # above through partition h. h runs west, so door_c's default side (the wall's left) is the hallway.
    def door(id, offset, width=0.9):
        return Opening(id=id, type="door", offset=offset, width=width, height=2.134)

    walls = [
        Wall(id="s", start=(0, 0), end=(6, 0), thickness=0.2, height=H),
        Wall(id="e", start=(6, 0), end=(6, 4), thickness=0.2, height=H, openings=[door("door_b", 0.25)]),
        Wall(id="n", start=(6, 4), end=(0, 4), thickness=0.2, height=H),
        Wall(id="w", start=(0, 4), end=(0, 0), thickness=0.2, height=H, openings=[door("door_a", 2.85)]),
        Wall(id="h", start=(6, 1.3), end=(0, 1.3), thickness=0.2, height=H, openings=[door("door_c", 2.6, 0.8)]),
    ]
    leaves = {o.id: o.leaf for o in build_scene(Building(ceiling_height=H, walls=walls)).openings}
    leaf = leaves["door_c"]
    assert leaf is not None
    opened = -leaf.rotation_y - leaf.swing  # plan angle of the open leaf
    assert math.sin(opened) > 0.9  # it opens into the room above, not across the 1.1 m hallway


def test_a_door_that_cannot_avoid_another_becomes_an_open_doorway():
    wall = Wall(id="s", start=(0, 0), end=(4, 0), thickness=0.2, height=H)
    wide, narrow = (Opening(id=i, type="door", offset=1.0, width=w, height=2.134) for i, w in (("a", 0.9), ("b", 0.8)))
    same = doors._swings(wall, wide, [])[0]
    group = [doors._Door(wall, wide, (1.45, 0), [same] * 4), doors._Door(wall, narrow, (1.4, 0), [same] * 4)]
    assert list(doors._settle_group(group)) == ["a"]  # the narrower one gives up its leaf
