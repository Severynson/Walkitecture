"""LLM-assisted ingestion: layer/block classification (§4.2) and review (§4.3).

Both return 503 `llm_unavailable` when no OpenAI key is configured or the call fails; the rule-based
pipeline and edits keep working without them.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Query

from app.api.deps import CurrentUser, SessionDep, SettingsDep
from app.api.errors import ApiError
from app.schemas.api import ClassifyRequest, ClassifyResponse, ParseResponse
from app.services import plan_store
from app.services.llm_classify import propose_classification
from app.services.reviewer import ReviewResult, review_plan

router = APIRouter(prefix="/api/plans/{plan_id}", tags=["ai"])


@router.post("/classify", response_model=ClassifyResponse)
async def classify(
    plan_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
    body: Annotated[ClassifyRequest | None, Body()] = None,
    apply: Annotated[bool, Query(description="Re-run the pipeline with the proposed parse options.")] = False,
) -> ClassifyResponse:
    """LLM proposal of layer roles and block categories, as parse options for POST /parse."""
    owner_id = user.id
    plan = await plan_store.get_plan(session, plan_id, owner_id, drawing=True)
    if plan.drawing is None:
        raise ApiError(409, "plan_not_parsed", "This plan has no drawing JSON yet. Run POST /parse first.")
    drawing = plan.drawing
    await session.rollback()  # don't hold a transaction open during the LLM call

    min_confidence = body.min_confidence if body else 0.5
    proposal = await propose_classification(drawing, settings, min_confidence=min_confidence)
    if not apply:
        return ClassifyResponse(proposal=proposal, applied=False)

    plan = await plan_store.get_plan(session, plan_id, owner_id, for_update=True, files=True)
    await plan_store.parse_plan(session, plan, proposal.parse_options, settings)
    result = ParseResponse(plan=plan_store.to_detail(plan), scene=plan_store.to_scene(plan))
    return ClassifyResponse(proposal=proposal, applied=True, result=result)


@router.post("/review", response_model=ReviewResult)
async def review(
    plan_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
    apply: Annotated[bool, Query(description="Apply valid patches (each one undoable).")] = False,
) -> ReviewResult:
    """Deterministic suspects -> LLM patches from the review allowlist -> validated (and applied) edits."""
    return await review_plan(session, plan_id, user.id, settings, apply=apply)
