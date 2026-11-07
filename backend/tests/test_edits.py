import pytest

from app.schemas.edits import EditCommandAdapter
from app.services.edits import EditRejected, apply_edit


def cmd(**raw):
    return EditCommandAdapter.validate_python(raw)


def window(building, window_id):
    return building.find_opening(window_id)[1]


def codes(exc_info) -> set[str]:
    return {p.code for p in exc_info.value.problems}


def test_resize_window_keeps_center_and_reports_affected(demo_building):
    before = window(demo_building, "window_1")
    result = apply_edit(
        demo_building, cmd(operation="resize_selected_window", targetId="window_1", parameters={"factor": 1.5})
    )
    after = window(result.building, "window_1")
    assert after.width == pytest.approx(before.width * 1.5)
    assert after.center == pytest.approx(before.center)
    assert result.affected_ids == ["window_1", "wall_1"]
    assert window(demo_building, "window_1").width == before.width  # input untouched


def test_resize_window_shifts_to_stay_inside_wall(demo_building):
    result = apply_edit(
        demo_building, cmd(operation="resize_selected_window", targetId="window_1", parameters={"width": 5.0})
    )
    after = window(result.building, "window_1")
    assert after.offset == 0.0 and after.width == 5.0
    assert "shifted" in result.summary


@pytest.mark.parametrize(
    ("params", "expected_code"),
    [
        ({"width": 8.0}, "openings_overlap"),
        ({"width": 11.0}, "opening_outside_wall"),
        ({"height": 2.5}, "opening_too_tall"),  # sill 2.5 ft (0.762) + 2.5 > 10 ft (3.048) - header
    ],
)
def test_invalid_window_resizes_are_rejected(demo_building, params, expected_code):
    with pytest.raises(EditRejected) as exc_info:
        apply_edit(demo_building, cmd(operation="resize_selected_window", targetId="window_1", parameters=params))
    assert expected_code in codes(exc_info)


def test_wall_with_single_window_resolves_to_that_window(demo_building):
    wall_4 = demo_building.find_wall("wall_4")
    (only_window,) = [o for o in wall_4.openings if o.type == "window"]
    result = apply_edit(
        demo_building, cmd(operation="resize_selected_window", targetId="wall_4", parameters={"factor": 1.2})
    )
    assert only_window.id in result.affected_ids


@pytest.mark.parametrize(("target", "code"), [("door_1", "not_a_window"), ("nope", "target_not_found")])
def test_resize_rejects_wrong_targets(demo_building, target, code):
    with pytest.raises(EditRejected) as exc_info:
        apply_edit(demo_building, cmd(operation="resize_selected_window", targetId=target, parameters={"factor": 2}))
    assert code in codes(exc_info)


def test_resize_all_windows(demo_building):
    result = apply_edit(demo_building, cmd(operation="resize_windows", parameters={"factor": 1.2}))
    widths = {o.id: o.width for w in result.building.walls for o in w.openings if o.type == "window"}
    original = {o.id: o.width for w in demo_building.walls for o in w.openings if o.type == "window"}
    assert widths == {k: pytest.approx(v * 1.2) for k, v in original.items()}


def test_resize_all_windows_is_atomic(demo_building):
    with pytest.raises(EditRejected):
        apply_edit(demo_building, cmd(operation="resize_windows", parameters={"factor": 6}))


def test_set_ceiling_height_updates_walls(demo_building):
    result = apply_edit(demo_building, cmd(operation="set_ceiling_height", parameters={"height": 3.0}))
    assert result.building.ceiling_height == 3.0
    assert {w.height for w in result.building.walls} == {3.0}
    assert result.affected_ids[0] == "building"


@pytest.mark.parametrize(("height", "code"), [(2.12, "opening_too_tall"), (9.0, "ceiling_height_out_of_range")])
def test_invalid_ceiling_heights_are_rejected(demo_building, height, code):
    with pytest.raises(EditRejected) as exc_info:
        apply_edit(demo_building, cmd(operation="set_ceiling_height", parameters={"height": height}))
    assert code in codes(exc_info)


def test_move_free_standing_wall(demo_building):
    result = apply_edit(
        demo_building, cmd(operation="move_selected_wall", targetId="wall_7", parameters={"offset": 0.5})
    )
    moved = result.building.find_wall("wall_7")
    assert moved.start == (2.0, 6.0) and moved.end == (4.0, 6.0)


@pytest.mark.parametrize(
    ("target", "offset", "code"),
    [
        ("wall_5", 0.3, "wall_not_independent"),
        ("wall_7", 2.45, "wall_collision"),
        ("window_1", 0.3, "not_a_wall"),
        ("wall_7", 9.0, "offset_too_large"),
    ],
)
def test_invalid_wall_moves_are_rejected(demo_building, target, offset, code):
    with pytest.raises(EditRejected) as exc_info:
        apply_edit(demo_building, cmd(operation="move_selected_wall", targetId=target, parameters={"offset": offset}))
    assert code in codes(exc_info)


def test_unknown_operation_is_not_a_valid_command():
    with pytest.raises(ValueError):
        EditCommandAdapter.validate_python({"operation": "delete_everything", "parameters": {}})
