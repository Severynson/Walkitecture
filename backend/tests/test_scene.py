"""Babylon.js scene spec: the ARCHITECTURE.md height rules and mesh layout."""

import math

import pytest

from app.schemas.building import Building, FurnitureItem, Opening, Wall
from app.services.babylon_scene import FRONT_ANGLE, build_scene
from app.services.furniture_catalog import CATALOG

FT = 0.3048


def y_span_ft(mesh) -> tuple[float, float]:
    return (
        round((mesh.position[1] - mesh.size[1] / 2) / FT, 3),
        round((mesh.position[1] + mesh.size[1] / 2) / FT, 3),
    )


def test_heights_follow_the_architecture_spec(demo_building):
    scene = build_scene(demo_building)
    assert scene.wall_height == pytest.approx(10 * FT)
    by_kind: dict[str, set] = {}
    for mesh in scene.meshes:
        if mesh.wall_id:
            by_kind.setdefault(mesh.kind, set()).add(y_span_ft(mesh))
    assert by_kind["wall"] == {(0.0, 10.0)}  # all walls the same height
    assert by_kind["sill"] == {(0.0, 2.5)}  # windows start at 2.5 ft ...
    assert by_kind["glass"] == {(2.5, 6.5)}  # ... and end at 6.5 ft
    # Headers: above doors from 7 ft, above windows from 6.5 ft, both up to the wall top.
    assert by_kind["header"] == {(7.0, 10.0), (6.5, 10.0)}


def test_wall_pieces_cover_the_wall_around_openings(demo_building):
    scene = build_scene(demo_building)
    wall = demo_building.find_wall("wall_1")  # 10 m, two windows, both ends connected
    pieces = [m for m in scene.meshes if m.wall_id == "wall_1" and m.kind in ("wall", "sill")]
    solid = sum(m.size[0] for m in pieces if m.kind == "wall")
    openings = sum(o.width for o in wall.openings)
    assert solid == pytest.approx(wall.length - openings + wall.thickness)  # + half thickness at both ends
    assert {m.size[2] for m in pieces} == {wall.thickness}
    assert all(m.rotation_y == 0.0 for m in pieces)  # wall runs along +x


def test_rotation_maps_local_x_onto_the_wall_direction():
    wall = Wall(id="w", start=(0, 0), end=(0, 4), thickness=0.2, height=3.0)
    (mesh,) = [m for m in build_scene(Building(ceiling_height=3.0, walls=[wall])).meshes if m.wall_id]
    a = mesh.rotation_y
    # Babylon RotationY maps local +x to (cos a, 0, -sin a); the wall points along +z (plan +y).
    assert (math.cos(a), -math.sin(a)) == pytest.approx((0.0, 1.0), abs=1e-6)
    assert mesh.position == (0.0, 1.5, 2.0)


def test_door_has_header_only_and_window_has_sill_header_and_glass():
    wall = Wall(
        id="w", start=(0, 0), end=(6, 0), thickness=0.2, height=10 * FT,
        openings=[
            Opening(id="d", type="door", offset=1, width=0.9, height=7 * FT),
            Opening(id="g", type="window", offset=3, width=1.2, height=4 * FT, sill_height=2.5 * FT),
        ],
    )  # fmt: skip
    scene = build_scene(Building(ceiling_height=10 * FT, walls=[wall]))
    kinds = {(m.opening_id, m.kind) for m in scene.meshes if m.opening_id}
    assert kinds == {("d", "header"), ("g", "header"), ("g", "sill"), ("g", "glass")}
    assert [o.id for o in scene.openings] == ["d", "g"]
    assert len({m.id for m in scene.meshes}) == len(scene.meshes)  # ids are unique


def test_furniture_spec_scales_and_turns_the_model():
    item = FurnitureItem(
        id="f", category="bed", model="bed_single_by_quaternius.glb", position=(2.0, 3.0), facing=(1.0, 0.0),
        width=1.0, depth=2.0, height=0.6,
    )  # fmt: skip
    (spec,) = build_scene(Building(ceiling_height=3.0, furniture=[item])).furniture
    assert spec.url == "/furniture/bed_single_by_quaternius.glb"
    assert spec.rotation_y == pytest.approx(math.pi / 2)  # local +z -> (sin a, cos a) = +x
    assert spec.scaling == pytest.approx((1.0 / 1.979, 0.6 / 1.355, 2.0 / 3.94), rel=1e-3)
    assert spec.footprint_center == (2.0, 0.0, 3.0)
    assert spec.position[1] == pytest.approx(0.0, abs=1e-3)  # model bottom on the floor


def test_camera_spawns_inside_a_room_at_eye_height(demo_building):
    camera = build_scene(demo_building).camera
    assert camera.body_height == pytest.approx(6 * FT)  # the walker is 6 ft tall
    assert camera.position[1] == camera.eye_height == pytest.approx(6 * FT - 0.12, abs=1e-3)
    assert 0 < camera.position[0] < 10 and 0 < camera.position[2] < 8


def test_camera_never_spawns_inside_furniture_or_a_wall():
    walls = [
        Wall(id="s", start=(0, 0), end=(6, 0), thickness=0.2, height=3),
        Wall(id="e", start=(6, 0), end=(6, 4), thickness=0.2, height=3),
        Wall(id="n", start=(6, 4), end=(0, 4), thickness=0.2, height=3),
        Wall(id="w", start=(0, 4), end=(0, 0), thickness=0.2, height=3),
        Wall(id="mid", start=(3, 0), end=(3, 4), thickness=0.2, height=3),  # through the plan's center
    ]
    bed = FurnitureItem(id="f1", category="bed", model="bed_king_by_quaternius.glb", position=(1.5, 2.0),
                        facing=(0.0, -1.0), width=1.9, depth=2.0, height=0.6)  # fmt: skip
    camera = build_scene(Building(ceiling_height=3, walls=walls, furniture=[bed])).camera  # no room labels
    x, z = camera.position[0], camera.position[2]
    assert abs(x - 3) > 0.4  # not in wall mid (the old fallback: the plan's center)
    assert not (0.55 <= x <= 2.45 and 1.0 <= z <= 3.0)  # not in the bed
    assert 0.4 < x < 5.6 and 0.4 < z < 3.6


def test_camera_spawns_in_the_most_open_room():
    from app.schemas.building import Room

    walls = [
        Wall(id="w1", start=(0, 0), end=(12, 0), thickness=0.15, height=3),
        Wall(id="w2", start=(12, 0), end=(12, 8), thickness=0.15, height=3),
        Wall(id="w3", start=(12, 8), end=(0, 8), thickness=0.15, height=3),
        Wall(id="w4", start=(0, 8), end=(0, 0), thickness=0.15, height=3),
        Wall(id="w5", start=(2, 0), end=(2, 8), thickness=0.15, height=3),
    ]
    rooms = [
        Room(id="hall", name="Hall", polygon=[(0, 0), (2, 0), (2, 8), (0, 8)], label_position=(1, 4)),
        Room(id="office", name="Office", polygon=[(2, 0), (12, 0), (12, 8), (2, 8)], label_position=(7, 4)),
    ]
    camera = build_scene(Building(ceiling_height=3, walls=walls, rooms=rooms)).camera
    assert (camera.position[0], camera.position[2]) == (7, 4)


# --- Wall joints: corners of mixed thickness, drafting gaps, free ends --------------------------


def _wall_extent(scene, wall_id):
    """(min, max) of the wall's boxes along its own axis, measured from its start (local x)."""
    lo, hi = math.inf, -math.inf
    for m in scene.meshes:
        if m.wall_id == wall_id and m.kind == "wall":
            lo, hi = min(lo, m.position[0] - m.size[0] / 2), max(hi, m.position[0] + m.size[0] / 2)
    return lo, hi


def test_corner_of_thick_and_thin_walls_is_filled():
    # Thick wall along x ending at the origin, thin wall along +y starting there.
    thick = Wall(id="thick", start=(-4, 0), end=(0, 0), thickness=0.24, height=3)
    thin = Wall(id="thin", start=(0, 0), end=(0, 3), thickness=0.07, height=3)
    scene = build_scene(Building(ceiling_height=3, walls=[thick, thin]))
    _, thick_end = _wall_extent(scene, "thick")
    assert thick_end == pytest.approx(0.035, abs=1e-3)  # reaches the thin wall's far face
    thin_boxes = [m for m in scene.meshes if m.wall_id == "thin" and m.kind == "wall"]
    assert min(m.position[2] - m.size[0] / 2 for m in thin_boxes) == pytest.approx(-0.12, abs=1e-3)  # thick's far face


def test_small_drafting_gap_is_closed_but_free_ends_stay():
    long_wall = Wall(id="long", start=(0, 0), end=(6, 0), thickness=0.2, height=3)
    near = Wall(id="near", start=(2, 0.25), end=(2, 3), thickness=0.1, height=3)  # stops 15 cm short of long's face
    free = Wall(id="free", start=(4, 1.5), end=(4, 3), thickness=0.1, height=3)  # 1.4 m from anything: a free end
    scene = build_scene(Building(ceiling_height=3, walls=[long_wall, near, free]))
    near_lo = min(m.position[2] - m.size[0] / 2 for m in scene.meshes if m.wall_id == "near" and m.kind == "wall")
    free_lo = min(m.position[2] - m.size[0] / 2 for m in scene.meshes if m.wall_id == "free" and m.kind == "wall")
    assert near_lo == pytest.approx(0.0, abs=1e-3)  # T-junction: reaches the long wall's centerline
    assert free_lo == pytest.approx(1.5, abs=1e-3)


def test_collinear_pieces_are_bridged():
    a = Wall(id="a", start=(0, 0), end=(2, 0), thickness=0.1, height=3)
    b = Wall(id="b", start=(2.12, 0), end=(4, 0), thickness=0.1, height=3)
    scene = build_scene(Building(ceiling_height=3, walls=[a, b]))
    assert _wall_extent(scene, "a")[1] == pytest.approx(2.12, abs=1e-3)


def test_furniture_keeps_its_proportions_when_the_block_is_oversized():
    # An "armchair with a person" block: 1.0 x 0.94 m for a 0.5 x 0.55 m chair model.
    item = FurnitureItem(
        id="f", category="chair", model="chair_by_quaternius.glb", position=(1, 1), facing=(0.0, 1.0),
        width=1.0, depth=0.94, height=0.9,
    )  # fmt: skip
    (spec,) = build_scene(Building(ceiling_height=3, furniture=[item])).furniture
    width, depth = spec.footprint
    stretch = (width / depth) / (0.5 / 0.55)
    assert 1 / 1.3**2 <= stretch <= 1.3**2  # each axis at most 1.3x off the chair's own proportions
    assert math.sqrt(width * depth / (0.5 * 0.55)) == pytest.approx(
        1.6, abs=1e-3
    )  # overall size capped at 1.6x the real chair


def test_corner_where_both_walls_stop_short_is_closed():
    # Drawn corner that doesn't meet: the vertical wall stops 15 cm below the horizontal one's line,
    # and the horizontal one stops 4 cm before the vertical one's line.
    horizontal = Wall(id="h", start=(0.04, 3.0), end=(4, 3.0), thickness=0.07, height=3)
    vertical = Wall(id="v", start=(0, 0), end=(0, 2.85), thickness=0.07, height=3)
    scene = build_scene(Building(ceiling_height=3, walls=[horizontal, vertical]))
    assert _wall_extent(scene, "h")[0] == pytest.approx(-0.035, abs=1e-3)  # x reaches v's far face (x = -0.035)
    v_top = max(m.position[2] + m.size[0] / 2 for m in scene.meshes if m.wall_id == "v" and m.kind == "wall")
    assert v_top == pytest.approx(3.035, abs=1e-3)  # reaches h's far face


def test_jogged_continuation_is_bridged():
    # Two in-line walls whose ends are 43 cm apart and shifted 19 cm sideways (bands still overlap).
    a = Wall(id="a", start=(0, 0), end=(3, 0), thickness=0.20, height=3)
    b = Wall(id="b", start=(3.43, 0.19), end=(6, 0.19), thickness=0.22, height=3)
    scene = build_scene(Building(ceiling_height=3, walls=[a, b]))
    assert _wall_extent(scene, "a")[1] == pytest.approx(3.43, abs=1e-3)


def _placed_box(model, spec):
    """World-space footprint (x/z extents) and front direction of a catalog model placed by `spec`."""
    nx, ny, nz = model.native_size
    cx, cy, cz = model.native_center
    sx, sy, sz = spec.scaling
    a = spec.rotation_y
    xs, zs = [], []
    for ux in (-0.5, 0.5):
        for uz in (-0.5, 0.5):
            # glTF -> Babylon (the loader mirrors x), then the spec's scale, rotation and position.
            lx, lz = -(cx + ux * nx) * sx, (cz + uz * nz) * sz
            xs.append(spec.position[0] + lx * math.cos(a) + lz * math.sin(a))
            zs.append(spec.position[2] - lx * math.sin(a) + lz * math.cos(a))
    phi = FRONT_ANGLE[model.front] + a  # the model's front, turned by RotationY
    return (min(xs), max(xs), min(zs), max(zs)), (math.sin(phi), math.cos(phi))


@pytest.mark.parametrize("model", CATALOG, ids=[m.file for m in CATALOG])
@pytest.mark.parametrize("facing", [(0.0, 1.0), (1.0, 0.0), (0.0, -1.0), (-1.0, 0.0)])
def test_every_library_model_fills_its_footprint_and_faces_forward(model, facing):
    """Whatever its native scale, origin, or front side, a model lands on its footprint, front first."""
    width, _, depth = model.real_size
    item = FurnitureItem(id="f", category=model.category, model=model.file, position=(3.0, 2.0), facing=facing,
                         width=width, depth=depth, height=model.real_size[1])  # fmt: skip
    spec = build_scene(Building(ceiling_height=3.048, furniture=[item])).furniture[0]
    (x0, x1, z0, z1), front = _placed_box(model, spec)
    across, along = (width, depth) if facing[1] else (depth, width)  # world x / z extents
    assert (x1 - x0, z1 - z0) == pytest.approx((across, along), abs=1e-3)
    assert ((x0 + x1) / 2, (z0 + z1) / 2) == pytest.approx((3.0, 2.0), abs=1e-3)
    assert front == pytest.approx(facing, abs=1e-6)


def test_a_wall_end_reaches_the_wall_ahead_not_one_meeting_it_from_the_side():
    """wall a ends 0.4 m above wall c; wall b (from the left) stops at a's face just above a's end."""
    walls = [
        Wall(id="a", start=(0.0, 3.0), end=(0.0, 0.4), thickness=0.25, height=3.0),
        Wall(id="b", start=(-2.0, 0.37), end=(-0.17, 0.37), thickness=0.08, height=3.0),
        Wall(id="c", start=(-0.05, 0.0), end=(3.0, 0.0), thickness=0.07, height=3.0),
    ]
    scene = build_scene(Building(ceiling_height=3.0, walls=walls))
    (piece,) = [m for m in scene.meshes if m.wall_id == "a"]
    bottom = piece.position[2] - piece.size[0] / 2
    assert bottom == pytest.approx(-0.035, abs=0.01)  # down to c's far face: no slit between a and c
