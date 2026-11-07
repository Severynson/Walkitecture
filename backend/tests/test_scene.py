"""Babylon.js scene spec: the ARCHITECTURE.md height rules and mesh layout."""

import math

import pytest

from app.schemas.building import Building, FurnitureItem, Opening, Wall
from app.services.babylon_scene import build_scene

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
    assert camera.position[1] == camera.eye_height == 1.6
    assert 0 < camera.position[0] < 10 and 0 < camera.position[2] < 8


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
