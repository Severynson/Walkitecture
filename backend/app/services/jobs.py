"""Background processing of uploads: DWG -> checked building model -> scene, with progress (ARCHITECTURE.md §4).

With the AI steps, a fresh drawing takes minutes (several LLM calls), far longer than a request should
wait. The upload returns at once with status "processing"; this job moves the plan through its stages
(`plan.stage`, shown by the frontend) and ends in "parsed" or "failed". No transaction stays open while
it waits for the LLM or a worker process. Jobs live in the server process: one interrupted by a restart
is marked failed at the next start (`fail_interrupted`), and POST /plans/{id}/process retries it.
"""

import asyncio
import logging
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from app.config import Settings
from app.models import Plan
from app.services import pipeline, plan_store
from app.services.dwg_convert import ConversionError, convert_dwg_to_dxf
from app.services.dxf_parser import ParseError

logger = logging.getLogger(__name__)
INTERRUPTED = "Processing was interrupted (the server restarted). Press Retry to process the drawing again."

_tasks: set[asyncio.Task] = set()


def start(sessions: async_sessionmaker[AsyncSession], plan_id: uuid.UUID, settings: Settings) -> None:
    """Process a plan in the background (its status must already be "processing")."""
    task = asyncio.create_task(process(sessions, plan_id, settings), name=f"process-plan-{plan_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def process(sessions: async_sessionmaker[AsyncSession], plan_id: uuid.UUID, settings: Settings) -> None:
    """The full pipeline with the AI steps, as one job. Never raises (failures are stored on the plan)."""

    async def stage(name: str) -> None:
        async with sessions() as session:
            await session.execute(update(Plan).where(Plan.id == plan_id).values(stage=name))
            await session.commit()

    try:
        async with sessions() as session:
            plan = await session.get(Plan, plan_id, options=[undefer(Plan.original_file), undefer(Plan.dxf_file)])
            if plan is None:
                return
            original, dxf, source_format = plan.original_file, plan.dxf_file, plan.source_format
        warnings: list[str] = []
        if dxf is None and source_format == "dwg":
            await stage("converting")
            dxf, warnings = await convert_dwg_to_dxf(original, settings)
            async with sessions() as session:
                await session.execute(update(Plan).where(Plan.id == plan_id).values(dxf_file=dxf))
                await session.commit()
        built = await pipeline.build_model(dxf if dxf is not None else original, None, settings, ai=True, stage=stage)
        await stage("saving")
        async with sessions() as session:
            plan = await session.get(Plan, plan_id, with_for_update=True)
            if plan is None:  # deleted meanwhile
                return
            await plan_store.store_built(session, plan, built, warnings)
            await session.commit()
    except (ConversionError, ParseError) as exc:
        await _fail(sessions, plan_id, str(exc))
    except asyncio.CancelledError:
        raise  # shutdown: fail_interrupted() marks it at the next start
    except Exception:
        logger.exception("Processing plan %s failed", plan_id)
        await _fail(sessions, plan_id, "Unexpected error while processing the drawing. Press Retry to try again.")


async def _fail(sessions: async_sessionmaker[AsyncSession], plan_id: uuid.UUID, message: str) -> None:
    try:
        async with sessions() as session:
            await session.execute(
                update(Plan).where(Plan.id == plan_id).values(status="failed", error=message, stage=None)
            )
            await session.commit()
    except Exception:
        logger.exception("Could not record the failure of plan %s", plan_id)


async def fail_interrupted(sessions: async_sessionmaker[AsyncSession]) -> int:
    """At startup: plans left "processing" by a previous server process can't finish any more."""
    async with sessions() as session:
        result = await session.execute(
            update(Plan).where(Plan.status == "processing").values(status="failed", error=INTERRUPTED, stage=None)
        )
        await session.commit()
        return result.rowcount or 0


async def shutdown() -> None:
    for task in list(_tasks):
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
