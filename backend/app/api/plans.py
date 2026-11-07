"""Plan upload (runs the whole DWG -> Babylon.js pipeline), drawing inspection, and scene endpoints."""

import uuid
from pathlib import PurePath
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Query, Response, UploadFile, status
from sqlalchemy import delete, select

from app.api.deps import CurrentUser, SessionDep, SettingsDep
from app.api.errors import ApiError
from app.models import Plan
from app.schemas.api import ParseResponse, PlanDetail, PlanSummary, ReplaceSceneRequest, SceneResponse
from app.schemas.parse import ParseOptions
from app.services import plan_store
from app.services.dwg_convert import ConversionError, detect_format
from app.services.dxf_parser import ParseError
from app.services.geometry import validate_building
from app.services.sample_plan import DEMO_FILENAME, build_demo_dxf

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PlanDetail)
async def upload_plan(
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
    file: Annotated[UploadFile, File(description="A .dwg or .dxf floor plan.")],
    parse: Annotated[bool, Query(description="Run the full pipeline immediately (default).")] = True,
) -> PlanDetail:
    """Upload a DWG/DXF. By default this runs the whole pipeline: DWG -> DXF -> descriptive drawing JSON ->
    building model -> Babylon.js scene, all stored for this plan. Parse failures are recorded on the plan."""
    data = await file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise ApiError(413, "file_too_large", f"Uploads are limited to {settings.max_upload_mb} MB.")
    if not data:
        raise ApiError(400, "empty_file", "The uploaded file is empty.")
    filename = PurePath(file.filename or "plan").name[:255] or "plan"
    fmt = detect_format(filename, data)
    if fmt is None:
        raise ApiError(415, "unsupported_file_type", "Only DWG and DXF files are supported.")

    plan = Plan(
        owner_id=user.id, filename=filename, source_format=fmt, file_size=len(data), original_file=data,
        parse_warnings=[],
    )  # fmt: skip
    session.add(plan)
    await session.commit()
    await session.refresh(plan)

    if parse:  # DWG conversion happens here; failures are stored as status=failed + error (ARCHITECTURE.md §11)
        plan = await plan_store.get_plan(session, plan.id, user.id, for_update=True, files=True)
        try:
            await plan_store.parse_plan(session, plan, ParseOptions(), settings)
        except (ConversionError, ParseError):
            await session.refresh(plan)
    return plan_store.to_detail(plan)


@router.post("/demo", status_code=status.HTTP_201_CREATED, response_model=ParseResponse)
async def create_demo_plan(session: SessionDep, settings: SettingsDep, user: CurrentUser) -> ParseResponse:
    """Create and parse the built-in two-bedroom sample plan (a known-good DXF)."""
    data = build_demo_dxf()
    plan = Plan(
        owner_id=user.id, filename=DEMO_FILENAME, source_format="dxf", file_size=len(data), original_file=data,
        parse_warnings=[],
    )  # fmt: skip
    session.add(plan)
    await session.commit()
    plan = await plan_store.get_plan(session, plan.id, user.id, for_update=True, files=True)
    await plan_store.parse_plan(session, plan, ParseOptions(), settings)
    return ParseResponse(plan=plan_store.to_detail(plan), scene=plan_store.to_scene(plan))


@router.get("", response_model=list[PlanSummary])
async def list_plans(
    session: SessionDep, user: CurrentUser, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[PlanSummary]:
    stmt = select(Plan).where(Plan.owner_id == user.id).order_by(Plan.created_at.desc()).limit(limit)
    return [plan_store.to_summary(p) for p in (await session.execute(stmt)).scalars()]


@router.get("/{plan_id}", response_model=PlanDetail)
async def get_plan(plan_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> PlanDetail:
    return plan_store.to_detail(await plan_store.get_plan(session, plan_id, user.id))


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_plan(plan_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> Response:
    await plan_store.get_plan(session, plan_id, user.id)
    await session.execute(delete(Plan).where(Plan.id == plan_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{plan_id}/parse", response_model=ParseResponse)
async def parse_plan(
    plan_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
    options: Annotated[ParseOptions | None, Body()] = None,
) -> ParseResponse:
    """(Re)run the pipeline with custom options (units, wall style, layer names...). Clears edit history."""
    plan = await plan_store.get_plan(session, plan_id, user.id, for_update=True, files=True)
    await plan_store.parse_plan(session, plan, options or ParseOptions(), settings)
    return ParseResponse(plan=plan_store.to_detail(plan), scene=plan_store.to_scene(plan))


@router.get("/{plan_id}/drawing")
async def get_drawing(
    plan_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    entities: Annotated[bool, Query(description="Include the per-entity list (can be large).")] = True,
) -> dict[str, Any]:
    """Descriptive JSON of the drawing: every entity's geometry and classification, layers, and blocks."""
    plan = await plan_store.get_plan(session, plan_id, user.id, drawing=True)
    if plan.drawing is None:
        raise ApiError(409, "plan_not_parsed", "This plan has not been parsed yet.")
    if entities:
        return plan.drawing
    return {k: v for k, v in plan.drawing.items() if k != "entities"}


@router.get("/{plan_id}/scene", response_model=SceneResponse)
async def get_scene(plan_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> SceneResponse:
    """The canonical building JSON plus the Babylon.js scene generated from it."""
    return plan_store.to_scene(await plan_store.get_plan(session, plan_id, user.id))


@router.put("/{plan_id}/scene", response_model=SceneResponse)
async def replace_scene(
    plan_id: uuid.UUID, body: ReplaceSceneRequest, session: SessionDep, user: CurrentUser
) -> SceneResponse:
    """Replace the building model with a hand-authored one (fully validated). Undoable."""
    problems = validate_building(body.model)
    if problems:
        raise ApiError(422, "invalid_model", "The building model failed validation.", problems)
    plan = await plan_store.replace_scene(session, plan_id, user.id, body.model, body.base_version)
    return plan_store.to_scene(plan)


@router.get("/{plan_id}/file")
async def download_file(
    plan_id: uuid.UUID,
    session: SessionDep,
    user: CurrentUser,
    kind: Annotated[Literal["original", "dxf"], Query(description="'dxf' returns the converted DXF.")] = "original",
) -> Response:
    """Download the uploaded file, or the DXF that a DWG was converted to."""
    plan = await plan_store.get_plan(session, plan_id, user.id, files=True)
    if kind == "dxf":
        content = plan.dxf_file if plan.dxf_file is not None else plan.original_file
        name = PurePath(plan.filename).with_suffix(".dxf").name
    else:
        content, name = plan.original_file, plan.filename
    return Response(
        content=content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"},
    )
