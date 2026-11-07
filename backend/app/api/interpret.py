"""Natural-language edit interpretation."""

from fastapi import APIRouter

from app.api.deps import CurrentUser, SessionDep, SettingsDep
from app.api.errors import ApiError
from app.schemas.api import EditPreview, InterpretRequest, InterpretResponse
from app.services import plan_store
from app.services.edits import EditRejected, apply_edit
from app.services.interpreter import interpret_edit

router = APIRouter(prefix="/api", tags=["interpret"])


@router.post("/interpret-edit", response_model=InterpretResponse)
async def interpret(
    body: InterpretRequest,
    session: SessionDep,
    settings: SettingsDep,
    user: CurrentUser,
) -> InterpretResponse:
    """Turn text + selected ID + model context into a typed edit command, validate it, and optionally apply it."""
    if body.apply and body.plan_id is None:
        raise ApiError(422, "missing_plan", "apply=true requires planId.")
    owner_id = user.id  # read before the rollback below expires ORM objects
    if body.plan_id is not None:
        plan = await plan_store.get_plan(session, body.plan_id, owner_id)
        building = plan_store.building_of(plan)
        await session.rollback()  # don't hold a transaction open during the LLM call
    elif body.model is not None:
        building = body.model
    else:
        raise ApiError(422, "missing_context", "Provide either planId or an inline model.")

    result = await interpret_edit(body.text, body.selected_id, building, settings, body.camera_forward)
    response = InterpretResponse(
        command=result.command,
        source=result.source,
        explanation=result.explanation,
        clarification=result.clarification,
        notes=result.notes,
    )
    if result.command is None:
        return response

    try:
        preview = apply_edit(building, result.command)
    except EditRejected as exc:
        response.valid, response.problems = False, exc.problems
        return response
    response.valid = True
    response.preview = EditPreview(affected_ids=preview.affected_ids, summary=preview.summary)

    if body.apply:
        response.applied = await plan_store.apply_and_record(
            session, body.plan_id, owner_id, result.command, base_version=body.base_version, source=result.source
        )
    return response
