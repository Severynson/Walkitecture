"""LLM classification (§4.2), review operations and reviewer (§4.3), with a fake OpenAI client."""

import io
from types import SimpleNamespace

import ezdxf
import pytest

from app.schemas.building import Building, FurnitureItem, Opening, Room, Wall
from app.schemas.edits import ReviewCommandAdapter
from app.schemas.parse import ParseOptions
from app.services import llm
from app.services.drawing_reader import describe_drawing
from app.services.dxf_parser import parse_dxf
from app.services.edits import EditRejected, apply_edit
from app.services.layers import layer_patterns
from app.services.llm_classify import _Answer as ClassifyAnswer
from app.services.llm_classify import propose_classification
from app.services.reviewer import _Answer as ReviewAnswer
from app.services.reviewer import _Patch, find_suspects
from app.services.sample_plan import build_demo_dxf


class FakeClient:
    """Stands in for AsyncOpenAI: returns a fixed parsed answer and records the request."""

    def __init__(self, answer):
        self.calls: list[dict] = []

        async def parse(**kwargs):
            self.calls.append(kwargs)
            message = SimpleNamespace(parsed=answer, refusal=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=parse))


def renamed_demo() -> bytes:
    """The demo apartment with meaningless layer/block names (WALLS -> XYZ-1, DOOR_900 -> B1, ...)."""
    doc = ezdxf.read(io.StringIO(build_demo_dxf().decode()))
    renames = {"WALLS": "XYZ-1", "WINDOWS": "XYZ-2", "DOORS": "XYZ-3"}
    for new in renames.values():
        doc.layers.add(new)
    doc.blocks.rename_block("DOOR_900", "B1")
    for entity in doc.modelspace():
        entity.dxf.layer = renames.get(entity.dxf.layer, entity.dxf.layer)
        if entity.dxftype() == "INSERT" and entity.dxf.name == "DOOR_900":
            entity.dxf.name = "B1"
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode()


def patch(operation, target_id=None, **fields):
    base = dict(wall_ids=None, opening_type=None, category=None, name=None, reason="test")
    return _Patch(operation=operation, target_id=target_id, **{**base, **fields})


# --- §4.2 classification ------------------------------------------------------------------


async def test_llm_classification_restores_a_drawing_with_unknown_layer_names(settings):
    data = renamed_demo()
    broken = parse_dxf(data, ParseOptions(), settings)
    assert broken.stats["doorCount"] == 0  # rules alone can't tell what B1 / XYZ-3 are

    answer = ClassifyAnswer(
        layers=[
            {"layer": "XYZ-1", "role": "wall", "confidence": 0.95},
            {"layer": "XYZ-2", "role": "window", "confidence": 0.9},
            {"layer": "XYZ-3", "role": "door", "confidence": 0.9},
            {"layer": "ROOMS", "role": "room", "confidence": 0.9},
            {"layer": "NOT-IN-FILE", "role": "wall", "confidence": 0.99},
            {"layer": "0", "role": "wall", "confidence": 0.2},
        ],
        blocks=[{"block": "B1", "role": "door", "confidence": 0.85}],
        notes="Layers look like walls/windows/doors by geometry.",
    )
    drawing = describe_drawing(data, layer_patterns(settings))
    client = FakeClient(answer)
    proposal = await propose_classification(drawing, settings, client=client)

    roles = {p.layer: p.role for p in proposal.layers}
    assert roles == {"XYZ-1": "wall", "XYZ-2": "window", "XYZ-3": "door", "ROOMS": "room"}
    assert len(proposal.dropped) == 2  # unknown layer + low confidence
    assert '"entities"' not in client.calls[0]["messages"][1]["content"]  # inventory only, never raw entities

    fixed = parse_dxf(data, proposal.parse_options, settings)
    assert (fixed.stats["wallCount"], fixed.stats["windowCount"], fixed.stats["doorCount"]) == (7, 5, 3)


def test_block_overrides_take_precedence(settings):
    as_windows = parse_dxf(build_demo_dxf(), ParseOptions(block_overrides={"door_900": "window"}), settings)
    assert (as_windows.stats["doorCount"], as_windows.stats["windowCount"]) == (0, 8)
    ignored = parse_dxf(build_demo_dxf(), ParseOptions(block_overrides={"DOOR_900": "ignore"}), settings)
    assert ignored.stats["doorCount"] == 0 and ignored.stats["windowCount"] == 5


async def test_classification_without_a_key_is_unavailable(settings):
    no_key = settings.model_copy(update={"openai_api_key": None})
    with pytest.raises(llm.LLMUnavailable):
        await propose_classification({"layers": [], "blocks": []}, no_key)


# --- §4.3 review operations -----------------------------------------------------------------


def cmd(**raw):
    return ReviewCommandAdapter.validate_python(raw)


def chair(item_id: str, x: float) -> FurnitureItem:
    return FurnitureItem(
        id=item_id, category="chair", model="chair_by_quaternius.glb", position=(x, 1), facing=(0, 1),
        width=0.5, depth=0.5, height=0.9,
    )  # fmt: skip


def split_wall_building() -> Building:
    window = Opening(id="window_1", type="window", offset=1, width=1, height=1.2, sill_height=0.762)
    return Building(
        ceiling_height=3.048,
        walls=[
            Wall(id="a", start=(0, 0), end=(3, 0), thickness=0.2, height=3.048),
            Wall(id="b", start=(6, 0), end=(3, 0), thickness=0.2, height=3.048, openings=[window]),  # reversed
            Wall(id="c", start=(0, 0), end=(0, 4), thickness=0.2, height=3.048),
        ],
        rooms=[Room(id="room_1", name="Room 1", polygon=[(0, 0), (6, 0), (6, 4), (0, 4)])],
        furniture=[chair("f1", 1.0), chair("f2", 1.05)],
    )


def test_merge_walls_keeps_openings_in_place():
    result = apply_edit(split_wall_building(), cmd(operation="merge_walls", parameters={"wallIds": ["a", "b"]}))
    merged = result.building.find_wall("a")
    assert result.building.find_wall("b") is None
    assert (merged.start, merged.end, merged.length) == ((0, 0), (6, 0), 6)
    (window,) = merged.openings
    assert (window.offset, window.width) == (4.0, 1)  # was 1 m from b's start at x=6 -> x 4..5
    assert set(result.affected_ids) == {"a", "b", "window_1"}


def test_merge_rejects_walls_that_are_not_collinear():
    with pytest.raises(EditRejected) as exc_info:
        apply_edit(split_wall_building(), cmd(operation="merge_walls", parameters={"wallIds": ["a", "c"]}))
    assert exc_info.value.problems[0].code == "walls_not_collinear"


def _opening(b: Building):
    return b.find_opening("window_1")[1]


@pytest.mark.parametrize(
    ("raw", "check"),
    [
        ({"operation": "delete_wall", "targetId": "b"}, lambda b: b.find_wall("b") is None),
        ({"operation": "delete_opening", "targetId": "window_1"}, lambda b: not b.find_wall("b").openings),
        (
            {"operation": "set_opening_type", "targetId": "window_1", "parameters": {"type": "door"}},
            lambda b: (_opening(b).type, _opening(b).sill_height) == ("door", 0),
        ),
        (
            {"operation": "set_furniture_category", "targetId": "f1", "parameters": {"category": "desk"}},
            lambda b: (b.furniture[0].model, b.furniture[0].matched_by) == ("desk_by_creativetrio.glb", "manual"),
        ),
        ({"operation": "delete_furniture", "targetId": "f2"}, lambda b: [f.id for f in b.furniture] == ["f1"]),
        (
            {"operation": "rename_room", "targetId": "room_1", "parameters": {"name": "  Living   room "}},
            lambda b: b.rooms[0].name == "Living room",
        ),
    ],
)
def test_review_operations(raw, check):
    result = apply_edit(split_wall_building(), cmd(**raw))
    assert check(result.building)


def test_review_operations_reject_unknown_targets():
    with pytest.raises(EditRejected) as exc_info:
        apply_edit(split_wall_building(), cmd(operation="delete_furniture", targetId="nope"))
    assert exc_info.value.problems[0].code == "target_not_found"


def test_suspects_are_found_deterministically():
    building = split_wall_building()
    building.walls.append(Wall(id="stub", start=(10, 10), end=(10.2, 10), thickness=0.1, height=3.048))
    warnings = ["2 window/door entities were not on any wall"]
    suspects = find_suspects(building, warnings, {"unresolvedFurnitureBlocks": ["LAVABO"]})
    kinds = {(s.kind, tuple(s.target_ids)) for s in suspects}
    assert ("short_wall", ("stub",)) in kinds
    assert ("overlapping_furniture", ("f1", "f2")) in kinds
    assert ("unlabelled_room", ("room_1",)) in kinds
    assert ("unsnapped_openings", ()) in kinds and ("unresolved_furniture_blocks", ()) in kinds


# --- API -------------------------------------------------------------------------------------


@pytest.fixture
def llm_answer(monkeypatch, settings):
    """Give the app a (fake) OpenAI key and make every LLM call return `holder['answer']`."""
    from app.config import get_settings
    from app.main import app

    holder: dict = {}
    keyed = settings.model_copy(update={"openai_api_key": "sk-test"})
    monkeypatch.setattr(llm, "make_client", lambda _settings: FakeClient(holder["answer"]))
    app.dependency_overrides[get_settings] = lambda: keyed
    return holder


async def test_classify_and_review_return_503_without_a_key(auth_client):
    plan_id = (await auth_client.post("/api/plans/demo")).json()["plan"]["id"]
    for path in ("classify", "review"):
        response = await auth_client.post(f"/api/plans/{plan_id}/{path}")
        assert response.status_code == 503, path
        assert response.json()["error"]["code"] == "llm_unavailable"


async def test_classify_endpoint_applies_the_proposal(auth_client, llm_answer):
    files = {"file": ("renamed.dxf", renamed_demo(), "application/dxf")}
    plan_id = (await auth_client.post("/api/plans", files=files)).json()["id"]
    roles = (("XYZ-1", "wall"), ("XYZ-2", "window"), ("XYZ-3", "door"), ("ROOMS", "room"))
    llm_answer["answer"] = ClassifyAnswer(
        layers=[{"layer": name, "role": role, "confidence": 0.9} for name, role in roles],
        blocks=[{"block": "B1", "role": "door", "confidence": 0.9}],
        notes="",
    )

    preview = (await auth_client.post(f"/api/plans/{plan_id}/classify")).json()
    assert preview["applied"] is False and preview["proposal"]["parseOptions"]["wallLayers"] == ["XYZ-1"]

    applied = (await auth_client.post(f"/api/plans/{plan_id}/classify?apply=true")).json()
    stats = applied["result"]["plan"]["parseStats"]
    assert (stats["wallCount"], stats["windowCount"], stats["doorCount"]) == (7, 5, 3)


async def test_review_endpoint_previews_applies_and_undoes(auth_client, llm_answer):
    plan_id = (await auth_client.post("/api/plans/demo")).json()["plan"]["id"]
    llm_answer["answer"] = ReviewAnswer(
        patches=[
            patch("delete_wall", "wall_7", reason="isolated free-standing fragment"),
            patch("rename_room", "room_99", name="Hall", reason="invented id"),
            patch("set_opening_type", "window_1", reason="missing parameter"),
        ],
        notes="One stray wall.",
    )

    preview = (await auth_client.post(f"/api/plans/{plan_id}/review")).json()
    assert any(s["kind"] == "isolated_wall" and s["targetIds"] == ["wall_7"] for s in preview["suspects"])
    assert [p["status"] for p in preview["patches"]] == ["valid", "rejected", "rejected"]
    assert preview["version"] == 1

    applied = (await auth_client.post(f"/api/plans/{plan_id}/review?apply=true")).json()
    assert [p["status"] for p in applied["patches"]] == ["applied", "rejected", "rejected"]
    scene = (await auth_client.get(f"/api/plans/{plan_id}/scene")).json()
    assert scene["version"] == 2 and all(w["id"] != "wall_7" for w in scene["model"]["walls"])
    history = (await auth_client.get(f"/api/plans/{plan_id}/edits")).json()
    assert [(h["command"]["operation"], h["source"]) for h in history] == [("delete_wall", "review")]

    undone = (await auth_client.post(f"/api/plans/{plan_id}/undo")).json()
    assert any(w["id"] == "wall_7" for w in undone["model"]["walls"])


async def test_review_ops_are_not_accepted_by_the_ui_edit_endpoint(auth_client):
    plan_id = (await auth_client.post("/api/plans/demo")).json()["plan"]["id"]
    response = await auth_client.post(
        f"/api/plans/{plan_id}/edits", json={"command": {"operation": "delete_wall", "targetId": "wall_1"}}
    )
    assert response.status_code == 422
