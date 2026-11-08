"""Automatic checks before the 3D scene is built (ARCHITECTURE.md §4.4): guardrails, AI decisions,
the upload pipeline and its background job, and the map's end in the scene."""

import asyncio
from types import SimpleNamespace

import pytest

from app.schemas.building import Building, FurnitureItem, Opening, Room, Wall
from app.schemas.edits import AnyCommandAdapter
from app.schemas.parse import ParseOptions
from app.services import guardrails, llm, pipeline, plan_store
from app.services.babylon_scene import MAP_MARGIN, build_scene, rendered_footprint
from app.services.edits import EditRejected, apply_edit
from app.services.furniture_catalog import CATALOG
from app.services.llm_classify import _Answer as ClassifyAnswer
from app.services.reviewer import _Answer as ReviewAnswer
from app.services.reviewer import _Decision, _Room
from app.services.sample_plan import build_demo_dxf, furniture_showroom

T, H = 0.2, 3.048
CLOSET, BED, CHAIR, DESK = (
    "closet_by_quaternius.glb", "bed_king_by_quaternius.glb", "chair_by_quaternius.glb", "desk_by_creativetrio.glb"
)  # fmt: skip


def wall(id, start, end, *openings):
    return Wall(id=id, start=start, end=end, thickness=T, height=H, openings=list(openings))


def door(id, offset, width=0.9):
    return Opening(id=id, type="door", offset=offset, width=width, height=2.13)


def window(id, offset, width=1.0):
    return Opening(id=id, type="window", offset=offset, width=width, height=1.2, sill_height=0.76)


def item(id, category, model, position, width, depth, facing=(0.0, -1.0)):
    return FurnitureItem(id=id, category=category, model=model, position=position, facing=facing, width=width,
                         depth=depth, height=1.0)  # fmt: skip


def two_rooms(middle: list[Opening] | None = None, furniture=(), extra_walls=()) -> Building:
    """8 x 4 m: 'Living' (x 0-4, front door in the south wall) and 'Bedroom' (x 4-8) split by wall 'mid'."""
    return Building(
        ceiling_height=H,
        walls=[
            wall("s", (0, 0), (8, 0), door("door_1", 1.0)),
            wall("e", (8, 0), (8, 4)),
            wall("n", (8, 4), (0, 4)),
            wall("w", (0, 4), (0, 0)),
            wall("mid", (4, 0), (4, 4), *(middle or [])),
            *extra_walls,
        ],
        rooms=[
            Room(id="room_1", name="Living", label_position=(2, 2)),
            Room(id="room_2", name="Bedroom", label_position=(6, 2)),
        ],  # fmt: skip
        furniture=list(furniture),
    )


# --- Furniture footprint --------------------------------------------------------------------------


@pytest.mark.parametrize(("width", "depth"), [(1.2, 0.6), (2.0, 0.4), (0.9, 0.9), (1.6, 0.45), (3.0, 1.0)])
def test_rendered_footprint_never_exceeds_the_drawing_and_is_idempotent(width, depth):
    closet = item("f", "closet", CLOSET, (0, 0), width, depth)
    w, d = rendered_footprint(closet)
    assert w <= width + 1e-9 and d <= depth + 1e-9
    again = rendered_footprint(closet.model_copy(update={"width": w, "depth": d}))
    assert again == pytest.approx((w, d))


def test_rendered_footprint_matches_the_drawing_when_proportions_are_believable():
    bed = item("f", "bed", BED, (0, 0), 1.8, 2.13)  # a 7 ft bed is drawn 7 ft, not 8
    assert rendered_footprint(bed) == pytest.approx((1.8, 2.13))


# --- The furniture rule ---------------------------------------------------------------------------


def test_furniture_cutting_into_a_wall_is_moved_just_clear_of_it():
    closet = item("f1", "closet", CLOSET, (2.0, 3.68), 1.2, 0.6)  # back edge 8 cm into wall n's face
    building = two_rooms(furniture=[closet])
    assert guardrails.find_conflicts(building) == [("f1", "n", pytest.approx(0.08, abs=0.002))]
    settled, fixes = guardrails.settle(building)
    (fix,) = fixes
    assert fix.action == "moved" and 0.07 <= fix.shift <= 0.1
    assert guardrails.find_conflicts(settled) == []
    moved = settled.furniture[0]
    assert moved.drawn.position == (2.0, 3.68) and (moved.width, moved.depth) == (1.2, 0.6)
    assert building.furniture[0].position == (2.0, 3.68)  # input untouched


def test_furniture_too_big_for_its_niche_shrinks_a_little():
    niche = wall("niche", (1.35, 4), (1.35, 3.2))  # 1.15 m between wall w and this wall
    closet = item("f1", "closet", CLOSET, (0.675, 3.6), 1.2, 0.6)
    settled, (fix,) = guardrails.settle(two_rooms(furniture=[closet], extra_walls=[niche]))
    assert fix.action == "resized" and fix.scale == 0.95
    assert guardrails.find_conflicts(settled) == []
    assert settled.furniture[0].width == pytest.approx(1.14)


def test_furniture_without_room_is_removed_and_the_bigger_piece_stays():
    beds = [item("f1", "bed", BED, (6.0, 2.0), 1.93, 2.03), item("f2", "bed", BED, (6.1, 2.0), 1.93, 2.03)]
    chair = item("f3", "chair", CHAIR, (6.0, 2.0), 0.5, 0.55)
    settled, fixes = guardrails.settle(two_rooms(furniture=[*beds, chair]))
    assert {f.item_id: f.action for f in fixes} == {"f2": "removed", "f3": "removed"}
    assert [f.id for f in settled.furniture] == ["f1"]


def test_a_chair_may_be_tucked_under_a_desk_but_not_into_it():
    desk = item("d", "desk", DESK, (2.0, 3.55), 1.4, 0.7)
    tucked = item("c", "chair", CHAIR, (2.0, 3.0), 0.5, 0.55, facing=(0.0, 1.0))  # 15 cm under the desk
    assert guardrails.find_conflicts(two_rooms(furniture=[desk, tucked])) == []
    inside = tucked.model_copy(update={"position": (2.0, 3.25)})  # 40 cm into it
    settled, (fix,) = guardrails.settle(two_rooms(furniture=[desk, inside]))
    assert fix.item_id == "c" and fix.action == "moved"
    assert guardrails.find_conflicts(settled) == []


def test_edits_cannot_push_a_wall_into_furniture():
    free = wall("free", (5.5, 1.0), (5.5, 3.0))
    chair = item("c", "chair", CHAIR, (6.0, 2.0), 0.5, 0.55, facing=(1.0, 0.0))
    building = two_rooms(furniture=[chair], extra_walls=[free])
    command = AnyCommandAdapter.validate_python(
        {"operation": "move_selected_wall", "targetId": "free", "parameters": {"offset": -0.3}}
    )
    result = plan_store._enforce(apply_edit(building, command))
    assert "c" in result.affected_ids and "Moved chair c" in result.summary
    assert guardrails.find_conflicts(result.building) == []


SOFA = "couch_medium_by_quaternius.glb"


def test_furniture_beside_a_door_moves_out_of_its_way():
    chair = item("c", "chair", CHAIR, (2.0, 0.6), 0.5, 0.55)  # 15 cm into the floor in front of door_1 (x 1-1.9)
    building = two_rooms(furniture=[chair])
    assert guardrails.find_conflicts(building) == [("c", "doorway door_1", pytest.approx(0.15, abs=0.002))]
    settled, (fix,) = guardrails.settle(building)
    assert fix.action == "moved" and fix.conflicts == ["doorway door_1"]
    assert settled.furniture[0].position[0] >= 2.15 - guardrails.TOUCH
    assert guardrails.find_conflicts(settled) == []


def test_furniture_blocking_a_door_with_nowhere_to_go_is_removed():
    sofa = item("s", "sofa", SOFA, (1.45, 0.7), 2.0, 0.9, facing=(0.0, 1.0))  # right inside the front door
    settled, (fix,) = guardrails.settle(two_rooms(furniture=[sofa]))
    assert fix.action == "removed" and "doorway door_1" in fix.summary
    assert settled.furniture == []


def test_a_door_is_kept_clear_on_both_sides_as_deep_as_it_is_wide():
    building = two_rooms([door("door_2", 1.5)])  # in wall mid (x 4), y 1.5-2.4: clear x 3.0-5.0
    behind = item("c", "chair", CHAIR, (5.2, 1.95), 0.5, 0.55, facing=(1.0, 0.0))  # bedroom side, 15 cm in
    assert [hit for _, hit, _ in guardrails.find_conflicts(building.model_copy(update={"furniture": [behind]}))] == [
        "doorway door_2"
    ]
    clear = behind.model_copy(update={"position": (5.3, 1.95)})
    assert guardrails.find_conflicts(building.model_copy(update={"furniture": [clear]})) == []


def test_a_door_added_by_an_edit_clears_its_way():
    closet = item("f1", "closet", CLOSET, (2.0, 3.68), 1.2, 0.6)
    command = AnyCommandAdapter.validate_python(
        {"operation": "add_opening", "targetId": "n", "parameters": {"type": "door", "offset": 5.55, "width": 0.9}}
    )  # wall n runs from x 8 to x 0: the door spans x 1.55-2.45, right behind the closet
    result = plan_store._enforce(apply_edit(two_rooms(furniture=[closet]), command))
    assert "f1" in result.affected_ids and "doorway" in result.summary
    assert guardrails.find_conflicts(result.building) == []


def test_furniture_leaves_a_clear_path_between_a_rooms_doors():
    # Living: front door_1 (x 1-1.9 in the south wall) and door_2 to the bedroom (y 1.5-2.4 in wall mid, x 4).
    # A row of closets across the room (x 0.1-3.3), clear of both doors' zones, leaves 0.6 m to wall mid.
    closets = [item("f1", "closet", CLOSET, (0.9, 1.25), 1.6, 0.4), item("f2", "closet", CLOSET, (2.5, 1.25), 1.6, 0.4)]
    building = two_rooms([door("door_2", 1.5)], furniture=closets)
    assert guardrails.find_conflicts(building) == []  # no wall, door zone or furniture is hit ...
    walkways, _, _ = guardrails._paths(building)
    assert walkways  # ... but the way from the front door to the bedroom is blocked
    walkable, fixes = guardrails.make_walkable(building)
    assert fixes and all("walkway door_1-door_2" in fix.summary for fix in fixes)
    assert guardrails._paths(walkable)[0] == []


def test_a_manual_edit_keeps_the_path_clear_too():
    closets = [item("f1", "closet", CLOSET, (0.9, 1.25), 1.6, 0.4), item("f2", "closet", CLOSET, (2.5, 1.25), 1.6, 0.4)]
    building = two_rooms(furniture=closets)  # no door to the bedroom yet: nothing to connect
    command = AnyCommandAdapter.validate_python(
        {"operation": "add_opening", "targetId": "mid", "parameters": {"type": "door", "offset": 1.5, "width": 0.9}}
    )
    result = plan_store._enforce(apply_edit(building, command))
    assert "walkway" in result.summary and guardrails._paths(result.building)[0] == []


def corridor(gap=0.75):
    """6 x 3 m: partition p (y = 0.1 + gap + 0.1, from the west wall to x 5) leaves a `gap` wide corridor along
    the south wall; door_a (west wall) opens into the corridor, door_b (north wall) into the room above."""
    y = 0.1 + gap + 0.1
    return Building(
        ceiling_height=H,
        walls=[wall("s", (0, 0), (6, 0)), wall("e", (6, 0), (6, 3)),
               wall("n", (6, 3), (0, 3), door("door_b", 4.0)),
               wall("w", (0, 3), (0, 0), door("door_a", 3 - 0.1 - gap, gap)),
               wall("p", (0, y), (5, y))],
        rooms=[Room(id="room_1", name="Hall", label_position=(3, 2))],
    )  # fmt: skip


def test_a_hallway_too_narrow_to_walk_is_widened():
    building = corridor()
    (finding,) = [f for f in guardrails.analyze(building).findings if f.kind == "narrow_passage"]
    assert "only 0.75 m wide between s and p" in finding.detail
    assert finding.default == "widen:p"  # into the room, not out of the facade
    assert [o.id for o in finding.options] == ["widen:p", "widen:s", "keep"]
    widened = guardrails.resolve(building, [finding], [], {}).building
    assert widened.find_wall("p").start[1] == pytest.approx(0.95 + 0.17)
    assert not [f for f in guardrails.analyze(widened).findings if f.kind == "narrow_passage"]


def test_a_hallway_wide_enough_is_left_alone():
    assert not [f for f in guardrails.analyze(corridor(0.95)).findings if f.kind == "narrow_passage"]


def test_shifting_a_wall_stretches_the_walls_that_meet_it():
    building = two_rooms([door("door_2", 1.5)])
    shift = AnyCommandAdapter.validate_python(
        {"operation": "shift_wall", "targetId": "mid", "parameters": {"offset": 0.5}}
    )
    result = apply_edit(building, shift)  # mid runs north: its left is west
    moved = result.building
    assert moved.find_wall("mid").start == (3.5, 0) and moved.find_wall("mid").end == (3.5, 4)
    assert moved.find_wall("s").start == (0, 0) and moved.find_wall("s").end == (8, 0)  # mid rests on s and n
    assert moved.find_wall("mid").openings[0].offset == 1.5
    corner = two_rooms()
    shift_w = AnyCommandAdapter.validate_python(
        {"operation": "shift_wall", "targetId": "w", "parameters": {"offset": -0.3}}
    )
    stretched = apply_edit(corner, shift_w).building  # w runs south: its left is east, so -0.3 moves it west
    assert stretched.find_wall("s").start == (-0.3, 0) and stretched.find_wall("n").end == (-0.3, 4)
    assert stretched.find_wall("s").openings[0].offset == pytest.approx(1.3)  # the front door stays put


def test_a_wall_meeting_another_at_an_angle_cannot_shift():
    slanted = wall("slant", (2, 0.1), (3, 1))
    building = two_rooms(extra_walls=[slanted])
    command = AnyCommandAdapter.validate_python(
        {"operation": "shift_wall", "targetId": "s", "parameters": {"offset": 0.2}}
    )
    with pytest.raises(EditRejected, match="angle"):
        apply_edit(building, command)


def test_the_furniture_showroom_holds_every_model_without_conflicts():
    showroom = furniture_showroom()
    assert {f.model for f in showroom.furniture} == {m.file for m in CATALOG}
    assert guardrails.find_conflicts(showroom) == []
    # One hall with a toilet beside a bed is exactly what the incompatible-furniture rule forbids in a real
    # plan; the showroom fixture is built without the guardrails, on purpose.
    assert [f.kind for f in guardrails.analyze(showroom).findings] == ["incompatible_furniture"]


# --- Judgement calls --------------------------------------------------------------------------------


def kinds(analysis):
    return [(f.kind, f.target_ids) for f in analysis.findings]


def test_a_sound_plan_has_no_findings():
    assert guardrails.analyze(two_rooms([door("door_2", 1.5)])).findings == []


def test_a_window_between_rooms_and_a_room_without_entrance_are_found():
    analysis = guardrails.analyze(two_rooms([window("window_1", 1.5)]))
    assert kinds(analysis) == [("enclosed_space", []), ("interior_window", ["window_1", "mid"])]
    enclosed, interior = analysis.findings
    assert "Bedroom" in enclosed.detail and enclosed.default == "keep"
    option_ids = [o.id for o in enclosed.options]
    assert option_ids[0] == "convert:window_1" and "keep" in option_ids
    assert any(o.startswith("door:mid@") for o in option_ids)
    assert [o.id for o in interior.options] == ["convert", "remove", "keep"]


def test_the_ai_decides_and_the_default_keeps_the_drawing():
    building = two_rooms([window("window_1", 1.5)])
    analysis = guardrails.analyze(building)
    enclosed = analysis.findings[0]

    kept = guardrails.resolve(building, analysis.findings, [], {})
    assert kept.building.find_opening("window_1")[1].type == "window"
    assert all(f.decided_by == "default" for f in kept.findings)

    decided = guardrails.resolve(building, analysis.findings, [], {enclosed.id: ("convert:window_1", "a door")})
    assert decided.building.find_opening("window_1")[1].type == "door"
    assert decided.findings[0].decided_by == "ai" and decided.findings[0].reason == "a door"
    assert any("window_1" in c.target_ids and c.source == "ai" for c in decided.changes)
    # Once the bedroom has a door, nothing is wrong any more.
    assert guardrails.analyze(decided.building).findings == []


def test_a_door_another_wall_stands_in_leads_nowhere():
    """A closet's doors cut into the partition, with a solid wall right behind them: the bedroom has no way in."""
    behind = wall("behind", (4.2, 0.8), (4.2, 3.2))  # against mid's east face, across the doorway
    building = two_rooms([door("door_2", 1.5)], extra_walls=[behind])
    assert guardrails.blocked_doors(building.walls) == {"door_2"}
    (enclosed,) = guardrails.analyze(building).findings
    assert enclosed.kind == "enclosed_space" and "Bedroom" in enclosed.detail
    # No option offers a way through the wall behind: no door into mid where it stands.
    for option in enclosed.options:
        for command in option.commands:
            if command["operation"] == "add_opening" and command["targetId"] == "mid":
                lo = command["parameters"]["offset"]
                assert lo + command["parameters"]["width"] < 0.8 or lo > 3.2
    assert guardrails.blocked_doors(two_rooms([door("door_2", 1.5)]).walls) == set()


def test_a_new_door_can_open_a_closed_room():
    analysis = guardrails.analyze(two_rooms())
    (enclosed,) = analysis.findings
    option = next(o for o in enclosed.options if o.id.startswith("door:mid@"))
    result = guardrails.resolve(two_rooms(), analysis.findings, [], {enclosed.id: (option.id, "missing door")})
    (added,) = result.building.find_wall("mid").openings
    assert added.type == "door" and added.width == pytest.approx(0.9)


def test_furniture_outside_the_building_is_removed_by_default():
    far = item("f1", "chair", CHAIR, (12.0, 2.0), 0.5, 0.55)
    near = item("f2", "table", "table_by_darwin_yamamoto.glb", (9.0, 2.0), 1.6, 0.9)  # a terrace, maybe
    analysis = guardrails.analyze(two_rooms([door("door_2", 1.5)], furniture=[far, near]))
    assert [(f.kind, f.target_ids, f.default) for f in analysis.findings] == [
        ("furniture_outside", ["f1"], "remove"),
        ("furniture_outside", ["f2"], "keep"),
    ]
    result = guardrails.resolve(two_rooms([door("door_2", 1.5)], furniture=[far, near]), analysis.findings, [], {})
    assert [f.id for f in result.building.furniture] == ["f2"]


def test_the_ai_can_keep_the_other_piece_in_a_conflict():
    closet = item("f1", "closet", CLOSET, (2.0, 3.6), 1.2, 0.6)
    chair = item("f2", "chair", CHAIR, (2.0, 3.1), 0.5, 0.55)  # in front of the closet, 20 cm into it
    building = two_rooms([door("door_2", 1.5)], furniture=[closet, chair])
    analysis = guardrails.analyze(building)
    (conflict,) = analysis.findings
    assert conflict.kind == "furniture_conflict" and conflict.target_ids == ["f2", "f1"]
    assert [o.id for o in conflict.options] == ["accept", "remove", "remove_other:f1"]
    result = guardrails.resolve(building, analysis.findings, [], {conflict.id: ("remove_other:f1", "duplicate")})
    assert [f.id for f in result.building.furniture] == ["f2"]
    assert result.building.furniture[0].position == (2.0, 3.1)  # nothing in its way any more


def test_a_hole_in_the_facade_is_closed_with_an_entrance_by_default():
    building = Building(
        ceiling_height=H,
        walls=[
            wall("s", (0, 0), (1, 0)),
            wall("e", (4, 0), (4, 4)),
            wall("n", (4, 4), (0, 4)),
            wall("w", (0, 4), (0, 0)),
        ],
    )  # the south wall stops 1 m from the corner: a 2.9 m hole
    (gap,) = guardrails.analyze(building).findings
    assert gap.kind == "facade_gap" and gap.default == "entrance"
    assert [o.id for o in gap.options] == ["entrance", "glazing", "wall", "keep"]
    closed = guardrails.resolve(building, [gap], [], {}).building
    (added,) = [w for w in closed.walls if w.id not in {"s", "e", "n", "w"}]
    assert added.start == (1, 0) and added.end == (4, 0)
    (door,) = added.openings
    assert door.type == "door" and door.width == pytest.approx(0.9)
    assert guardrails.analyze(closed).findings == []
    glazed = guardrails.resolve(building, [gap], [], {gap.id: ("glazing", "living room")}).building
    assert [o.type for w in glazed.walls for o in w.openings] == ["window"]


def test_a_gap_between_two_rooms_is_a_passage_not_a_hole():
    building = two_rooms([door("door_2", 1.5)], extra_walls=[wall("stub", (8, 2), (6.5, 2))])
    assert [f for f in guardrails.analyze(building).findings if f.kind == "facade_gap"] == []


def bath_and_bedroom(furniture, door_between=True):
    """Bathroom (x 0-3) and bedroom (x 3-6); the wall between stops 1 m short of the north wall."""
    middle = [wall("mid1", (3, 0), (3, 2.2), *([door("door_2", 0.6, 0.8)] if door_between else [])),
              wall("mid2", (3, 3.2), (3, 4))]  # fmt: skip
    return Building(
        ceiling_height=H,
        walls=[wall("s", (0, 0), (6, 0), door("door_1", 4.0)), wall("e", (6, 0), (6, 4)), wall("n", (6, 4), (0, 4)),
               wall("w", (0, 4), (0, 0)), *middle],
        rooms=[Room(id="room_1", name="Bedroom", label_position=(4.5, 2))],
        furniture=furniture,
    )  # fmt: skip


def test_a_bathroom_with_a_door_cannot_also_be_open_to_another_room():
    toilet = item("f1", "toilet", "toilet_by_quaternius.glb", (0.5, 2.0), 0.4, 0.7, facing=(1.0, 0.0))
    building = bath_and_bedroom([toilet])
    (finding,) = [f for f in guardrails.analyze(building).findings if f.kind == "bathroom_open"]
    assert finding.default == "wall" and [o.id for o in finding.options] == ["wall", "door"]  # no "keep": it has a door
    assert "Bedroom" in finding.detail and "1.00 m" in finding.detail
    sealed = guardrails.resolve(building, [finding], [], {}).building
    assert not [f for f in guardrails.analyze(sealed).findings if f.kind == "bathroom_open"]


def test_a_bathroom_without_doors_may_stay_open_and_other_rooms_are_not_checked():
    toilet = item("f1", "toilet", "toilet_by_quaternius.glb", (0.5, 2.0), 0.4, 0.7, facing=(1.0, 0.0))
    (finding,) = [f for f in guardrails.analyze(bath_and_bedroom([toilet], door_between=False)).findings
                  if f.kind == "bathroom_open"]  # fmt: skip
    assert finding.default == "door" and "keep" in [o.id for o in finding.options]
    living = item("f1", "sofa", "couch_medium_by_quaternius.glb", (1.5, 2.0), 2.0, 0.9, facing=(1.0, 0.0))
    assert not [f for f in guardrails.analyze(bath_and_bedroom([living])).findings if f.kind == "bathroom_open"]


# --- The scene: nobody walks or falls off the map --------------------------------------------------


def test_the_map_ends_in_backdrop_walls_with_sky_above():
    scene = build_scene(two_rooms([door("door_2", 1.5)]))
    by_kind: dict[str, list] = {}
    for mesh in scene.meshes:
        by_kind.setdefault(mesh.kind, []).append(mesh)
    assert len(by_kind["boundary"]) == 4 and len(by_kind["ground"]) == 1 and len(by_kind["sky"]) == 1
    assert scene.map_bounds.min[0] == pytest.approx(scene.bounds.min[0] - MAP_MARGIN)
    assert scene.map_bounds.max[2] == pytest.approx(scene.bounds.max[2] + MAP_MARGIN)
    ground = by_kind["ground"][0]
    assert ground.size[0] > scene.map_bounds.max[0] - scene.map_bounds.min[0]  # reaches under the boundary walls
    assert scene.materials["boundary"].texture == "/map-end-background.jpg"
    assert scene.materials["sky"].texture == "/sky.avif"
    assert all(scene.materials[k].texture is None for k in ("wall", "header", "sill", "ceiling", "floor"))
    assert by_kind["sky"][0].position[1] > scene.wall_height  # high above the house's own ceiling


# --- Pipeline with a fake LLM -----------------------------------------------------------------------


class ScriptedClient:
    """Fake AsyncOpenAI answering by the requested schema (classification, review...)."""

    def __init__(self, answers: dict):
        self.calls: list[type] = []
        self.requests: list[dict] = []

        async def parse(**kwargs):
            schema = kwargs["response_format"]
            self.calls.append(schema)
            self.requests.append(kwargs)
            answer = answers.get(schema)
            if isinstance(answer, Exception):
                raise answer
            message = SimpleNamespace(parsed=answer, refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=parse))


@pytest.fixture
def keyed(settings):
    return settings.model_copy(update={"openai_api_key": "sk-test"})


async def test_pipeline_runs_classification_and_review_before_the_model_is_built(monkeypatch, keyed):
    client = ScriptedClient({
        ClassifyAnswer: ClassifyAnswer(layers=[], blocks=[], notes="Layer names already clear."),
        ReviewAnswer: ReviewAnswer(
            rooms=[_Room(room="Bedroom 1", drawing="a bed, a door", model="no furniture, a door", matches=False,
                         note="the bed block was not recognised")],
            patches=[], decisions=[_Decision(finding_id="F9", option_id="x", reason="?")], notes="Looks fine.",
        ),
    })  # fmt: skip
    monkeypatch.setattr(llm, "make_client", lambda _settings: client)
    stages: list[str] = []

    async def stage(name):
        stages.append(name)

    built = await pipeline.build_model(build_demo_dxf(), None, keyed, ai=True, stage=stage)
    assert built.error is None and built.result.building.walls
    assert stages == ["reading", "classifying", "checking", "rendering", "reviewing", "fixing"]
    assert client.calls == [ClassifyAnswer, ReviewAnswer]
    # The review sees two pictures: the original drawing and the model built from it.
    content = client.requests[1]["messages"][1]["content"]
    pictures = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
    assert len(pictures) == 2 and all(url.startswith("data:image/png;base64,iVBOR") for url in pictures)
    report = built.report
    assert [(r.room, r.matches) for r in report.visual] == [("Bedroom 1", False)]
    assert report.ai == "used" and report.classification.notes == "Layer names already clear."
    assert "AI review: Looks fine." in report.notes
    assert any("F9" in note for note in report.notes)  # an unknown finding id is ignored, not applied


async def test_pipeline_survives_ai_failures(monkeypatch, keyed):
    broken = llm.LLMUnavailable("timeout")
    client = ScriptedClient({ClassifyAnswer: broken, ReviewAnswer: broken})
    monkeypatch.setattr(llm, "make_client", lambda _settings: client)
    built = await pipeline.build_model(build_demo_dxf(), None, keyed, ai=True)
    assert built.error is None and built.result.building.walls
    assert built.report.ai == "unavailable"
    assert any("layer classification unavailable" in note for note in built.report.notes)


async def test_pipeline_without_ai(settings):
    built = await pipeline.build_model(build_demo_dxf(), ParseOptions(), settings, ai=False)
    assert built.report.ai == "off" and built.report.classification is None


# --- Background processing (API) ---------------------------------------------------------------------


async def test_uploads_are_processed_in_the_background(auth_client, settings):
    from app.config import get_settings
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: settings.model_copy(update={"process_in_background": True})
    files = {"file": ("demo.dxf", build_demo_dxf(), "application/dxf")}
    created = (await auth_client.post("/api/plans", files=files)).json()
    assert created["status"] == "processing" and created["stage"] == "queued"
    plan_id = created["id"]
    busy = await auth_client.post(f"/api/plans/{plan_id}/process")
    assert busy.status_code == 409 or busy.json()["status"] == "processing"

    for _ in range(200):
        plan = (await auth_client.get(f"/api/plans/{plan_id}")).json()
        if plan["status"] != "processing":
            break
        await asyncio.sleep(0.05)
    assert plan["status"] == "parsed", plan
    assert plan["stage"] is None and plan["report"]["ai"] == "unavailable"  # no key in tests
    scene = (await auth_client.get(f"/api/plans/{plan_id}/scene")).json()
    assert scene["babylon"]["mapBounds"]


async def test_retry_processes_the_drawing_again(auth_client):
    files = {"file": ("demo.dxf", build_demo_dxf(), "application/dxf")}
    plan = (await auth_client.post("/api/plans", files=files)).json()
    assert plan["status"] == "parsed" and plan["report"]["findings"] == []
    again = await auth_client.post(f"/api/plans/{plan['id']}/process")
    assert again.status_code == 202 and again.json()["status"] == "parsed"
    assert again.json()["modelVersion"] == plan["modelVersion"] + 1


def test_a_kitchen_sink_never_shares_a_room_with_a_toilet():
    toilet = item("f1", "toilet", "toilet_by_quaternius.glb", (0.5, 1.0), 0.4, 0.7, facing=(1.0, 0.0))
    sink = item("f2", "sink", "kitchen_sink_by_kay_lousberg.glb", (2.0, 3.4), 1.2, 0.7)  # a washbasin, misread
    building = two_rooms([door("door_2", 1.5)], furniture=[toilet, sink])
    building.rooms[0].name = "Bathroom"
    (finding,) = [f for f in guardrails.analyze(building).findings if f.kind == "incompatible_furniture"]
    assert finding.default == "remove_other" and "kitchen sink f2" in finding.detail
    assert [f.id for f in guardrails.resolve(building, [finding], [], {}).building.furniture] == ["f1"]


def test_open_plan_kitchens_and_studios_are_fine():
    fridge = item("f1", "fridge", "kitchen_fridge_quarternius.glb", (0.5, 3.5), 0.7, 0.7)
    bed = item("f2", "bed", BED, (2.5, 2.0), 1.93, 2.03)
    sofa = item("f3", "sofa", "couch_medium_by_quaternius.glb", (1.2, 0.8), 2.0, 0.9, facing=(0.0, 1.0))
    building = two_rooms([door("door_2", 1.5)], furniture=[fridge, bed, sofa])
    assert not [f for f in guardrails.analyze(building).findings if f.kind == "incompatible_furniture"]


FRIDGE, NIGHTSTAND = "kitchen_fridge_quarternius.glb", "night_stand_quaternius.glb"


def misplaced(building):
    return [f for f in guardrails.analyze(building).findings if f.kind == "misplaced_furniture"]


def test_furniture_that_does_not_belong_in_its_room_is_removed():
    fridge = item("f1", "fridge", FRIDGE, (6.5, 3.5), 0.7, 0.7)
    bed = item("f2", "bed", BED, (6.0, 1.5), 1.93, 2.03)
    building = two_rooms([door("door_2", 1.5)], furniture=[fridge, bed])  # a fridge in the Bedroom
    (finding,) = misplaced(building)
    assert finding.target_ids == ["f1"] and finding.default == "remove" and "Bedroom (a bedroom)" in finding.detail
    assert [f.id for f in guardrails.resolve(building, [finding], [], {}).building.furniture] == ["f2"]


def test_a_bed_where_the_label_says_kitchen_is_kept_when_nothing_else_says_kitchen():
    bed = item("f1", "bed", BED, (2.0, 2.0), 1.93, 2.03)
    building = two_rooms([door("door_2", 1.5)], furniture=[bed])
    building.rooms[0].name = "Cocina"  # probably the label of the room next door
    (finding,) = misplaced(building)
    assert finding.default == "keep" and "nothing else there is typical" in finding.detail


def test_open_plan_rooms_studios_and_unlabelled_rooms_are_not_judged():
    fridge = item("f1", "fridge", FRIDGE, (0.5, 3.5), 0.7, 0.7)
    sofa = item("f2", "sofa", SOFA, (2.0, 2.0), 2.0, 0.9)
    bed = item("f3", "bed", BED, (6.0, 1.5), 1.93, 2.03)
    nightstand = item("f4", "nightstand", NIGHTSTAND, (7.5, 3.5), 0.45, 0.4)
    building = two_rooms([door("door_2", 1.5)], furniture=[fridge, sofa, bed, nightstand])
    building.rooms[0].name = "Salón-cocina"
    building.rooms[1].name = "Studio"
    assert misplaced(building) == []
    building.rooms[1].name = "Trastero"  # storage: not a room type the rule judges
    assert misplaced(building) == []


def test_a_toilet_beside_a_fridge_is_one_finding_not_two():
    toilet = item("f1", "toilet", "toilet_by_quaternius.glb", (0.5, 3.0), 0.4, 0.7, facing=(1.0, 0.0))
    fridge = item("f2", "fridge", FRIDGE, (2.5, 3.5), 0.7, 0.7)
    building = two_rooms([door("door_2", 1.5)], furniture=[toilet, fridge])
    building.rooms[0].name = "Kitchen"
    kinds = [f.kind for f in guardrails.analyze(building).findings]
    assert kinds.count("incompatible_furniture") == 1 and "misplaced_furniture" not in kinds


def three_sided_room(name="Living room"):
    """5 x 4 m room whose south wall is missing entirely; a sofa stands in it."""
    sofa = item("f1", "sofa", "couch_medium_by_quaternius.glb", (2.5, 3.0), 2.0, 0.9)
    return Building(
        ceiling_height=H,
        walls=[wall("w", (0, 0), (0, 4), window("window_1", 1.5, 1.0)), wall("n", (0, 4), (5, 4)),
               wall("e", (5, 4), (5, 0), window("window_2", 1.5, 1.0))],
        rooms=[Room(id="room_1", name=name, label_position=(2.5, 2.0))],
        furniture=[sofa],
    )  # fmt: skip


def test_a_room_missing_its_whole_outer_wall_gets_a_wider_window_by_default():
    building = three_sided_room()
    (finding,) = [f for f in guardrails.analyze(building).findings if f.kind == "missing_wall"]
    assert finding.default == "window" and "Living room with sofa f1" in finding.detail
    assert [o.id for o in finding.options] == ["window", "door", "wall", "keep"]
    closed = guardrails.resolve(building, [finding], [], {}).building
    (added,) = [w for w in closed.walls if w.id not in {"w", "n", "e"}]
    (glass,) = added.openings
    assert glass.type == "window" and glass.width == pytest.approx(1.3)  # 1.3 x the typical 1.0 m window
    assert not [f for f in guardrails.analyze(closed).findings if f.kind == "missing_wall"]


def test_a_terrace_stays_open_by_default():
    (finding,) = [f for f in guardrails.analyze(three_sided_room("Terraza")).findings if f.kind == "missing_wall"]
    assert finding.default == "keep"


def test_the_drawing_and_the_model_are_rendered_for_the_visual_review(settings):
    from app.services.dxf_parser import parse_dxf
    from app.services.plan_images import render_pair

    dxf = build_demo_dxf()
    result = parse_dxf(dxf, ParseOptions(), settings)
    drawing, model = render_pair(dxf, result.building, result.stats["unitFactor"], tuple(result.stats["planOrigin"]))
    assert drawing.startswith(b"\x89PNG") and model.startswith(b"\x89PNG")
    assert len(drawing) > 5_000 and len(model) > 5_000  # not blank
