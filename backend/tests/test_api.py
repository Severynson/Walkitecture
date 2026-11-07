"""End-to-end API tests against PostgreSQL (skipped when the database is unreachable)."""

import io
import uuid

import ezdxf

from app.services.sample_plan import build_demo_dxf

RESIZE = {"operation": "resize_selected_window", "targetId": "window_1", "parameters": {"factor": 1.5}}


async def create_demo(client) -> dict:
    response = await client.post("/api/plans/demo")
    assert response.status_code == 201, response.text
    return response.json()


async def test_health(auth_client):
    body = (await auth_client.get("/api/health")).json()
    assert body["database"] == "ok"
    assert body["interpreter"] == "rules"


async def test_demo_plan_round_trip(auth_client):
    created = await create_demo(auth_client)
    plan_id = created["plan"]["id"]
    assert created["plan"]["status"] == "parsed"
    assert created["plan"]["parseStats"]["wallCount"] == 7

    scene = (await auth_client.get(f"/api/plans/{plan_id}/scene")).json()
    assert scene["version"] == 1
    assert scene["model"]["schemaVersion"] == 1
    assert scene["model"]["walls"][0]["openings"][0].keys() >= {"id", "type", "offset", "width", "sillHeight"}

    listing = (await auth_client.get("/api/plans")).json()
    assert any(p["id"] == plan_id for p in listing)


async def test_edit_history_and_undo(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    original = (await auth_client.get(f"/api/plans/{plan_id}/scene")).json()["model"]

    body = {"command": RESIZE, "baseVersion": 1, "source": "ui"}
    edit = await auth_client.post(f"/api/plans/{plan_id}/edits", json=body)
    assert edit.status_code == 200, edit.text
    assert edit.json()["version"] == 2
    assert edit.json()["affectedIds"] == ["window_1", "wall_1"]

    history = (await auth_client.get(f"/api/plans/{plan_id}/edits")).json()
    assert [h["command"]["operation"] for h in history] == ["resize_selected_window"]
    assert history[0]["source"] == "ui"

    undo = await auth_client.post(f"/api/plans/{plan_id}/undo")
    assert undo.status_code == 200
    assert undo.json()["version"] == 3
    assert undo.json()["model"] == original

    again = await auth_client.post(f"/api/plans/{plan_id}/undo")
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "nothing_to_undo"


async def test_stale_version_conflicts(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    response = await auth_client.post(f"/api/plans/{plan_id}/edits", json={"command": RESIZE, "baseVersion": 99})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "version_conflict"


async def test_dry_run_does_not_persist(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    preview = await auth_client.post(f"/api/plans/{plan_id}/edits?dryRun=true", json={"command": RESIZE})
    assert preview.json()["applied"] is False
    assert (await auth_client.get(f"/api/plans/{plan_id}/scene")).json()["version"] == 1


async def test_rejected_edit_returns_problems(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    command = {"operation": "set_ceiling_height", "parameters": {"height": 2.12}}
    response = await auth_client.post(f"/api/plans/{plan_id}/edits", json={"command": command})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "edit_rejected"
    assert {p["code"] for p in body["problems"]} == {"opening_too_tall"}


async def test_unknown_operation_is_a_validation_error(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    response = await auth_client.post(
        f"/api/plans/{plan_id}/edits", json={"command": {"operation": "run_js", "parameters": {"code": "alert(1)"}}}
    )
    assert response.status_code == 422


async def test_upload_dxf_and_parse(auth_client):
    dxf = build_demo_dxf()  # not byte-stable across calls (ezdxf writes timestamps)
    files = {"file": ("apartment.dxf", dxf, "application/dxf")}
    uploaded = await auth_client.post("/api/plans?parse=false", files=files)
    assert uploaded.status_code == 201, uploaded.text
    plan = uploaded.json()
    assert plan["status"] == "uploaded" and plan["sourceFormat"] == "dxf" and not plan["hasModel"]

    not_ready = await auth_client.get(f"/api/plans/{plan['id']}/scene")
    assert not_ready.status_code == 409

    parsed = await auth_client.post(f"/api/plans/{plan['id']}/parse", json={"ceilingHeight": 3.0})
    assert parsed.status_code == 200, parsed.text
    assert parsed.json()["scene"]["model"]["ceilingHeight"] == 3.0

    download = await auth_client.get(f"/api/plans/{plan['id']}/file")
    assert download.content == dxf


async def test_upload_with_parse_failure_records_error(auth_client):
    doc = ezdxf.new()
    doc.modelspace().add_circle((0, 0), 1)
    stream = io.StringIO()
    doc.write(stream)
    files = {"file": ("empty.dxf", stream.getvalue().encode(), "application/dxf")}
    response = await auth_client.post("/api/plans?parse=true", files=files)
    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert "No wall geometry" in response.json()["error"]


async def test_upload_rejects_unsupported_files(auth_client):
    response = await auth_client.post("/api/plans", files={"file": ("plan.png", b"\x89PNG\r\n\x1a\n", "image/png")})
    assert response.status_code == 415
    assert response.json()["error"]["code"] == "unsupported_file_type"


async def test_failed_dwg_conversion_is_recorded_on_the_plan(auth_client):
    """ARCHITECTURE.md §11: the plan is kept with status=failed and the reason (with or without dwg2dxf)."""
    files = {"file": ("broken.dwg", b"AC1032" + b"\x00" * 64, "image/vnd.dwg")}
    response = await auth_client.post("/api/plans", files=files)
    assert response.status_code == 201
    plan = response.json()
    assert plan["status"] == "failed" and plan["error"] and not plan["hasModel"]

    reparse = await auth_client.post(f"/api/plans/{plan['id']}/parse")
    assert reparse.status_code == 422
    assert reparse.json()["error"]["code"] == "dwg_conversion_failed"
    listed = {p["id"]: p["status"] for p in (await auth_client.get("/api/plans")).json()}
    assert listed[plan["id"]] == "failed"


async def test_replace_scene_validates_and_is_undoable(auth_client):
    created = await create_demo(auth_client)
    plan_id = created["plan"]["id"]
    model = created["scene"]["model"]

    bad = {**model, "ceilingHeight": 1.0}
    response = await auth_client.put(f"/api/plans/{plan_id}/scene", json={"model": bad})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_model"

    smaller = {**model, "walls": model["walls"][:4]}
    response = await auth_client.put(f"/api/plans/{plan_id}/scene", json={"model": smaller})
    assert response.status_code == 200 and len(response.json()["model"]["walls"]) == 4

    undone = await auth_client.post(f"/api/plans/{plan_id}/undo")
    assert len(undone.json()["model"]["walls"]) == 7


async def test_interpret_edit_previews_and_applies(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    body = {"text": "Make the selected window 50% wider.", "selectedId": "window_1", "planId": plan_id}

    preview = (await auth_client.post("/api/interpret-edit", json=body)).json()
    assert preview["source"] == "rules"
    assert preview["valid"] is True and preview["applied"] is None
    assert preview["command"]["parameters"] == {"factor": 1.5, "width": None, "height": None, "sillHeight": None}

    applied = (await auth_client.post("/api/interpret-edit", json={**body, "apply": True, "baseVersion": 1})).json()
    assert applied["applied"]["version"] == 2


async def test_interpret_edit_reports_invalid_commands(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    body = {"text": "Set the ceiling to 2.12 m", "planId": plan_id, "apply": True}
    result = (await auth_client.post("/api/interpret-edit", json=body)).json()
    assert result["valid"] is False
    assert result["applied"] is None
    assert result["problems"][0]["code"] == "opening_too_tall"


async def test_interpret_edit_with_inline_model(auth_client, demo_building):
    body = {"text": "raise the ceiling to 3 m", "model": demo_building.model_dump(mode="json", by_alias=True)}
    result = (await auth_client.post("/api/interpret-edit", json=body)).json()
    assert result["valid"] is True


async def test_missing_plan_is_404(auth_client):
    response = await auth_client.get(f"/api/plans/{uuid.uuid4()}/scene")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "plan_not_found"


async def test_delete_plan(auth_client):
    plan_id = (await create_demo(auth_client))["plan"]["id"]
    assert (await auth_client.delete(f"/api/plans/{plan_id}")).status_code == 204
    assert (await auth_client.get(f"/api/plans/{plan_id}")).status_code == 404
