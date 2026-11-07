"""Validated edits, edit history, and undo."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.schemas.api import EditHistoryItem, EditRequest, EditResponse
from app.services import plan_store

router = APIRouter(prefix="/api/plans/{plan_id}", tags=["edits"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.post("/edits", response_model=EditResponse)
async def apply_edit(
    plan_id: uuid.UUID,
    body: EditRequest,
    session: SessionDep,
    dry_run: Annotated[bool, Query(alias="dryRun", description="Validate and preview without saving.")] = False,
) -> EditResponse:
    """Apply one allowlisted edit command. Returns the new model plus the IDs whose meshes must be rebuilt."""
    return await plan_store.apply_and_record(
        session, plan_id, body.command, base_version=body.base_version, source=body.source, dry_run=dry_run
    )


@router.get("/edits", response_model=list[EditHistoryItem])
async def edit_history(plan_id: uuid.UUID, session: SessionDep) -> list[EditHistoryItem]:
    edits = await plan_store.list_edits(session, plan_id)
    return [
        EditHistoryItem(
            id=e.id,
            version=e.version,
            command=e.command,
            affected_ids=e.affected_ids,
            summary=e.summary,
            source=e.source,
            created_at=e.created_at,
        )
        for e in edits
    ]


@router.post("/undo", response_model=EditResponse)
async def undo(plan_id: uuid.UUID, session: SessionDep) -> EditResponse:
    """Revert the most recent edit."""
    return await plan_store.undo_last(session, plan_id)
