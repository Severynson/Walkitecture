"""Validated edits, edit history, and undo."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, SessionDep
from app.schemas.api import EditHistoryItem, EditRequest, EditResponse
from app.services import plan_store

router = APIRouter(prefix="/api/plans/{plan_id}", tags=["edits"])


@router.post("/edits", response_model=EditResponse)
async def apply_edit(
    plan_id: uuid.UUID,
    body: EditRequest,
    session: SessionDep,
    user: CurrentUser,
    dry_run: Annotated[bool, Query(alias="dryRun", description="Validate and preview without saving.")] = False,
) -> EditResponse:
    """Apply one allowlisted edit. Returns the new model, its Babylon scene, and the IDs to rebuild."""
    return await plan_store.apply_and_record(
        session, plan_id, user.id, body.command, base_version=body.base_version, source=body.source, dry_run=dry_run
    )


@router.get("/edits", response_model=list[EditHistoryItem])
async def edit_history(plan_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> list[EditHistoryItem]:
    edits = await plan_store.list_edits(session, plan_id, user.id)
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
async def undo(plan_id: uuid.UUID, session: SessionDep, user: CurrentUser) -> EditResponse:
    """Revert the most recent edit."""
    return await plan_store.undo_last(session, plan_id, user.id)
