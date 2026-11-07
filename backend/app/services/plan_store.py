"""Database operations on plans: loading, parsing, applying edits with history, and undo."""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.config import Settings
from app.models import Plan, PlanEdit
from app.schemas.api import EditResponse, PlanDetail, PlanSummary, SceneResponse
from app.schemas.building import Building
from app.schemas.edits import EditCommand
from app.schemas.parse import ParseOptions
from app.services.dwg_convert import convert_dwg_to_dxf
from app.services.dxf_parser import ParseError, ParseResult, parse_dxf
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


async def get_plan(session: AsyncSession, plan_id: uuid.UUID, *, for_update: bool = False, files: bool = False) -> Plan:
    stmt = select(Plan).where(Plan.id == plan_id)
    if files:
        stmt = stmt.options(undefer(Plan.original_file), undefer(Plan.dxf_file))
    if for_update:
        stmt = stmt.with_for_update()
    plan = (await session.execute(stmt)).scalar_one_or_none()
    if plan is None:
        raise PlanNotFound(f"Plan {plan_id} not found.")
    return plan


def building_of(plan: Plan) -> Building:
    if plan.model is None:
        raise PlanNotParsed(f"Plan {plan.id} has no scene yet. Call POST /api/plans/{plan.id}/parse first.")
    return Building.model_validate(plan.model)


def dump_building(building: Building) -> dict:
    return building.model_dump(mode="json", by_alias=True)


def to_summary(plan: Plan) -> PlanSummary:
    return PlanSummary(
        id=plan.id,
        filename=plan.filename,
        source_format=plan.source_format,
        file_size=plan.file_size,
        status=plan.status,
        model_version=plan.model_version,
        has_model=plan.model is not None,
        error=plan.error,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def to_detail(plan: Plan) -> PlanDetail:
    return PlanDetail(
        **to_summary(plan).model_dump(), parse_warnings=plan.parse_warnings or [], parse_stats=plan.parse_stats
    )


def to_scene(plan: Plan) -> SceneResponse:
    return SceneResponse(plan_id=plan.id, version=plan.model_version, model=building_of(plan))


async def parse_plan(session: AsyncSession, plan: Plan, options: ParseOptions, settings: Settings) -> ParseResult:
    """(Re)parse the plan's DXF into a fresh model. Clears edit history (IDs may change).

    `plan` must have been loaded with files=True and for_update=True.
    """
    warnings: list[str] = []
    if plan.dxf_file is None and plan.source_format == "dwg":
        plan.dxf_file, warnings = await convert_dwg_to_dxf(plan.original_file, settings)
    dxf = plan.dxf_file if plan.dxf_file is not None else plan.original_file
    try:
        result = parse_dxf(dxf, options, settings)
    except ParseError as exc:
        plan.status, plan.error = "failed", str(exc)
        await session.commit()
        raise
    result.warnings[:0] = warnings

    await session.execute(delete(PlanEdit).where(PlanEdit.plan_id == plan.id))
    plan.model = dump_building(result.building)
    plan.model_version += 1
    plan.status, plan.error = "parsed", None
    plan.parse_warnings = result.warnings
    plan.parse_stats = result.stats
    await session.commit()
    await session.refresh(plan)
    return result


def preview_edit(plan: Plan, command: EditCommand) -> EditResult:
    return apply_edit(building_of(plan), command)


async def apply_and_record(
    session: AsyncSession,
    plan_id: uuid.UUID,
    command: EditCommand,
    *,
    base_version: int | None,
    source: str,
    dry_run: bool = False,
) -> EditResponse:
    plan = await get_plan(session, plan_id, for_update=not dry_run)
    if base_version is not None and base_version != plan.model_version:
        raise VersionConflict(plan.model_version)
    result = preview_edit(plan, command)
    if dry_run:
        return EditResponse(
            plan_id=plan.id, applied=False, version=plan.model_version,
            affected_ids=result.affected_ids, summary=result.summary, model=result.building,
        )  # fmt: skip

    previous = plan.model
    plan.model = dump_building(result.building)
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
        plan_id=plan_id, applied=True, version=version,
        affected_ids=result.affected_ids, summary=result.summary, model=result.building,
    )  # fmt: skip


async def replace_scene(
    session: AsyncSession, plan_id: uuid.UUID, building: Building, base_version: int | None
) -> Plan:
    plan = await get_plan(session, plan_id, for_update=True)
    if base_version is not None and base_version != plan.model_version:
        raise VersionConflict(plan.model_version)
    previous = plan.model
    plan.model = dump_building(building)
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


async def undo_last(session: AsyncSession, plan_id: uuid.UUID) -> EditResponse:
    plan = await get_plan(session, plan_id, for_update=True)
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
    plan.model = last.previous_model
    plan.model_version += 1  # versions only move forward, so stale clients still get a 409
    version = plan.model_version
    summary = f"Undid: {last.summary}"
    affected = list(last.affected_ids)
    await session.delete(last)
    await session.commit()
    return EditResponse(
        plan_id=plan_id, applied=True, version=version,
        affected_ids=affected, summary=summary, model=restored,
    )  # fmt: skip


async def list_edits(session: AsyncSession, plan_id: uuid.UUID) -> list[PlanEdit]:
    await get_plan(session, plan_id)
    stmt = select(PlanEdit).where(PlanEdit.plan_id == plan_id).order_by(PlanEdit.id)
    return list((await session.execute(stmt)).scalars())
