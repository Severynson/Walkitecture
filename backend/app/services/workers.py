"""CPU-heavy drawing work (describe + build, guardrail checks) runs in worker processes, never in the web
server's event loop.

Reading a large DWG is seconds-to-minutes of pure-Python CPU. Run inline, it froze the single uvicorn
event loop: health checks timed out, Docker marked the container unhealthy, and Traefik stopped routing
to it, so one 44k-entity upload took the whole site down for ~3.5 minutes (2026-11-08). In a separate
process the server stays responsive, a semaphore keeps concurrent parses within the worker count, and a
time limit kills a runaway parse instead of letting it occupy a worker forever.
"""

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from app.config import Settings
from app.schemas.parse import ParseOptions
from app.services.classify import LayerPatterns
from app.services.drawing_reader import describe_drawing
from app.services.dxf_parser import ParseError, ParseResult, parse_dxf

logger = logging.getLogger(__name__)

_pool: ProcessPoolExecutor | None = None
_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None


class DrawingTooComplex(ParseError):
    """The drawing could not be read within PARSE_TIMEOUT_S."""


def _read(dxf: bytes, patterns: LayerPatterns, options: ParseOptions, settings: Settings):
    """Runs in a worker process. Returns (drawing JSON, ParseResult or the ParseError it raised).

    The drawing JSON is returned even when building fails, so a failed plan can still be classified.
    DrawingReadError (unreadable file) propagates as is.
    """
    drawing = describe_drawing(dxf, patterns)
    try:
        return drawing, parse_dxf(dxf, options, settings)
    except ParseError as exc:
        return drawing, exc


def _get_pool(settings: Settings) -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        # "spawn": a fresh interpreter per worker (forking a process that runs an event loop and DB
        # connections is unsafe).
        _pool = ProcessPoolExecutor(max_workers=settings.parse_workers, mp_context=multiprocessing.get_context("spawn"))
    return _pool


def _get_slots(settings: Settings) -> asyncio.Semaphore:
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:  # one per event loop (tests run several loops)
        _slots, _slots_loop = asyncio.Semaphore(settings.parse_workers), loop
    return _slots


def _kill_pool() -> None:
    """Terminate the workers (a timed-out parse cannot be cancelled otherwise) and start fresh next time."""
    global _pool
    pool, _pool = _pool, None
    if pool is None:
        return
    for process in list(getattr(pool, "_processes", {}).values()):  # no public API to kill a busy worker
        process.terminate()
    pool.shutdown(wait=False, cancel_futures=True)


async def run(settings: Settings, fn, *args):
    """Run a picklable, module-level function in a worker process, within settings.parse_timeout_s."""
    async with _get_slots(settings):
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(_get_pool(settings), fn, *args)
            return await asyncio.wait_for(future, timeout=settings.parse_timeout_s)
        except TimeoutError as exc:
            logger.warning("Drawing work exceeded %.0f s; killing the worker pool.", settings.parse_timeout_s)
            _kill_pool()
            raise DrawingTooComplex(
                f"The drawing is too complex to read within {settings.parse_timeout_s:.0f} s. "
                "Try a smaller drawing or remove unneeded layers (hatches, details, other sheets)."
            ) from exc
        except BrokenProcessPool as exc:
            logger.exception("Drawing worker crashed")
            _kill_pool()
            raise ParseError("The drawing reader crashed (the file may be corrupt or too large).") from exc


async def read_drawing(
    dxf: bytes, patterns: LayerPatterns, options: ParseOptions, settings: Settings
) -> tuple[dict, ParseResult | ParseError]:
    """Describe and build a drawing in a worker process, within settings.parse_timeout_s."""
    return await run(settings, _read, dxf, patterns, options, settings)


def shutdown() -> None:
    """Stop the worker processes (application shutdown)."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
