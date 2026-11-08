"""Database operations on plans: loading (owner-scoped), running the pipeline, edits with history, undo.

Upload pipeline (ARCHITECTURE.md): DWG -> DXF -> descriptive drawing JSON -> canonical building
model (walls, openings, rooms, furniture) -> automatic checks (pipeline.py, guardrails.py) ->
Babylon.js scene spec, all written to the database. Uploads run it in the background (jobs.py).
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.config import Settings
from app.models import Plan, PlanEdit
from app.schemas.api import EditResponse, PlanDetail, PlanSummary, SceneResponse
from app.schemas.babylon import BabylonScene
from app.schemas.building import Building
from app.schemas.checks import CheckReport
from app.schemas.edits import AnyCommand
from app.schemas.parse import ParseOptions
from app.services import guardrails, pipeline
from app.services.babylon_scene import build_scene
from app.services.dwg_convert import ConversionError, convert_dwg_to_dxf
from app.services.dxf_parser import ParseError, ParseResult
from app.services.edits import EditResult, apply_edit


class PlanNotFound(Exception):
    pass


class PlanNotParsed(Exception):
    pass


class VersionConflict(Exception):
    def __init__(self, current: int):
        self.current = current
        super().__init__(f"Plan was modified (current version {current}). Reload the scene and retry.")


class NothingToUndo(Exception):
    pass


class PlanBusy(Exception):
    """The plan is being (re)processed; its model will be replaced when that finishes."""


async def get_plan(
    session: AsyncSession,
    plan_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    for_update: bool = False,
    files: bool = False,
    drawing: bool = False,
) -> Plan:
    stmt = select(Plan).where(Plan.id == plan_id, Plan.owner_id == owner_id)
    if files:
        stmt = stmt.options(undefer(Plan.original_file), undefer(Plan.dxf_file))
    if drawing:
        stmt = stmt.options(undefer(Plan.drawing))
    if for_update:
        stmt = stmt.with_for_update()
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if plan is None:  # other users' plans are indistinguishable from missing ones
        raise PlanNotFound(f"Plan {plan_id} not found.")
    return plan


def building_of(plan: Plan) -> Building:
    if plan.model is None:
        raise PlanNotParsed(f"Plan {plan.id} has no scene yet. Call POST /api/plans/{plan.id}/parse first.")
    return Building.model_validate(plan.model)


def dump_building(building: Building) -> dict:
    return building.model_dump(mode="json", by_alias=True)


def dump_scene(scene: BabylonScene) -> dict:
    return scene.model_dump(mode="json", by_alias=True)


def _set_model(plan: Plan, building: Building) -> BabylonScene:
    """Store a new model and its regenerated Babylon scene; returns the scene."""
    scene = build_scene(building)
    plan.model = dump_building(building)
    plan.scene = dump_scene(scene)
    return scene


def to_summary(plan: Plan) -> PlanSummary:
    return PlanSummary(
        id=plan.id,
        filename=plan.filename,
        source_format=plan.source_format,
        file_size=plan.file_size,
        status=plan.status,
        stage=plan.stage if plan.status == "processing" else None,
        model_version=plan.model_version,
        has_model=plan.model is not None,
        error=plan.error,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def to_detail(plan: Plan) -> PlanDetail:
    return PlanDetail(
        **to_summary(plan).model_dump(),
        parse_warnings=plan.parse_warnings or [],
        parse_stats=plan.parse_stats,
        report=CheckReport.model_validate(plan.report) if plan.report else None,
    )


def to_scene(plan: Plan) -> SceneResponse:
    building = building_of(plan)
    return SceneResponse(plan_id=plan.id, version=plan.model_version, model=building, babylon=build_scene(building))


async def parse_plan(
    session: AsyncSession, plan: Plan, options: ParseOptions | None, settings: Settings, *, ai: bool = False
) -> ParseResult:
    """Run the whole pipeline now and store every stage. Clears edit history (IDs may change).

    `plan` must have been loaded with files=True and for_update=True. `options=None` lets the AI choose
    the layer roles (with `ai`). Uploads use jobs.py instead, which runs this in the background.
    """
    warnings: list[str] = []
    try:
        if plan.dxf_file is None and plan.source_format == "dwg":
            plan.dxf_file, warnings = await convert_dwg_to_dxf(plan.original_file, settings)
        dxf = plan.dxf_file if plan.dxf_file is not None else plan.original_file
        built = await pipeline.build_model(dxf, options, settings, ai=ai)
    except (ConversionError, ParseError) as exc:
        plan.status, plan.error, plan.stage = "failed", str(exc), None  # ARCHITECTURE.md §11: keep the plan
        await session.commit()
        raise
    await store_built(session, plan, built, warnings)
    await session.commit()
    await session.refresh(plan)
    if built.error:
        raise built.error
    return built.result


async def store_built(session: AsyncSession, plan: Plan, built: pipeline.Built, conversion_warnings: list[str]) -> None:
    """Write a pipeline result to `plan` (not committed). A failed build keeps the drawing JSON, so the
    plan can still be inspected and re-parsed with other options."""
    plan.drawing = built.drawing
    plan.parse_options = built.options.model_dump(mode="json", by_alias=True, exclude_none=True)
    plan.report = built.report.model_dump(mode="json", by_alias=True)
    plan.stage = None
    if built.error or built.result is None:
        plan.status, plan.error = "failed", str(built.error)
        return
    result = built.result
    result.warnings[:0] = conversion_warnings
    await session.execute(delete(PlanEdit).where(PlanEdit.plan_id == plan.id))
    _set_model(plan, result.building)
    plan.model_version += 1
    plan.status, plan.error = "parsed", None
    plan.parse_warnings = result.warnings
    plan.parse_stats = result.stats


def _check_idle(plan: Plan) -> None:
    if plan.status == "processing":
        raise PlanBusy("The drawing is still being processed; try again when it is ready.")


def _enforce(result: EditResult) -> EditResult:
    """Edits must not break the furniture rule either (a moved wall pushes furniture out, or removes it)."""
    building, fixes = guardrails.enforce(result.building)
    if not fixes:
        return result
    notes = " ".join(fix.summary for fix in fixes)
    return EditResult(
        building=building,
        affected_ids=[*result.affected_ids, *(fix.item_id for fix in fixes)],
        summary=f"{result.summary} {notes}",
    )


def preview_edit(plan: Plan, command: AnyCommand) -> EditResult:
    return _enforce(apply_edit(building_of(plan), command))


async def apply_and_record(
    session: AsyncSession,
    plan_id: uuid.UUID,
    owner_id: uuid.UUID,
    command: AnyCommand,
    *,
    base_version: int | None,
    source: str,
    dry_run: bool = False,
) -> EditResponse:
    plan = await get_plan(session, plan_id, owner_id, for_update=not dry_run)
    _check_idle(plan)
    if base_version is not None and base_version != plan.model_version:
        raise VersionConflict(plan.model_version)
    result = preview_edit(plan, command)
    if dry_run:
        return EditResponse(
            plan_id=plan.id, applied=False, version=plan.model_version, affected_ids=result.affected_ids,
            summary=result.summary, model=result.building, babylon=build_scene(result.building),
        )  # fmt: skip

    previous = plan.model
    scene = _set_model(plan, result.building)
    plan.model_version += 1
    session.add(
        PlanEdit(
            plan_id=plan.id,
            version=plan.model_version,
            command=command.model_dump(mode="json", by_alias=True),
            previous_model=previous,
            affected_ids=result.affected_ids,
            summary=result.summary,
            source=source,
        )
    )
    version = plan.model_version
    await session.commit()
    return EditResponse(
        plan_id=plan_id, applied=True, version=version, affected_ids=result.affected_ids,
        summary=result.summary, model=result.building, babylon=scene,
    )  # fmt: skip


async def replace_scene(
    session: AsyncSession, plan_id: uuid.UUID, owner_id: uuid.UUID, building: Building, base_version: int | None
) -> Plan:
    plan = await get_plan(session, plan_id, owner_id, for_update=True)
    _check_idle(plan)
    if base_version is not None and base_version != plan.model_version:
        raise VersionConflict(plan.model_version)
    previous = plan.model
    building, _ = guardrails.enforce(building)
    _set_model(plan, building)
    plan.model_version += 1
    plan.status, plan.error = "parsed", None
    if previous is not None:
        session.add(
            PlanEdit(
                plan_id=plan.id,
                version=plan.model_version,
                command={"operation": "replace_scene"},
                previous_model=previous,
                affected_ids=["building"],
                summary="Replaced the whole scene.",
                source="api",
            )
        )
    await session.commit()
    await session.refresh(plan)
    return plan


async def undo_last(session: AsyncSession, plan_id: uuid.UUID, owner_id: uuid.UUID) -> EditResponse:
    plan = await get_plan(session, plan_id, owner_id, for_update=True)
    _check_idle(plan)
    stmt = (
        select(PlanEdit)
        .where(PlanEdit.plan_id == plan.id)
        .order_by(PlanEdit.id.desc())
        .limit(1)
        .options(undefer(PlanEdit.previous_model))
    )
    last = (await session.execute(stmt)).scalar_one_or_none()
    if last is None:
        raise NothingToUndo("There is nothing to undo.")
    restored = Building.model_validate(last.previous_model)
    scene = _set_model(plan, restored)
    plan.model_version += 1  # versions only move forward, so stale clients still get a 409
    version = plan.model_version
    summary = f"Undid: {last.summary}"
    affected = list(last.affected_ids)
    await session.delete(last)
    await session.commit()
    return EditResponse(
        plan_id=plan_id, applied=True, version=version, affected_ids=affected,
        summary=summary, model=restored, babylon=scene,
    )  # fmt: skip


async def list_edits(session: AsyncSession, plan_id: uuid.UUID, owner_id: uuid.UUID) -> list[PlanEdit]:
    await get_plan(session, plan_id, owner_id)
    stmt = select(PlanEdit).where(PlanEdit.plan_id == plan_id).order_by(PlanEdit.id)
    return list((await session.execute(stmt)).scalars())
