"""From a DXF to a checked building model (ARCHITECTURE.md §4): every step before the 3D scene is built.

    read       describe the drawing + build walls/openings/rooms/furniture with the rule-based roles
    classify   (AI) layer and block roles from the drawing's inventory; rebuild when they differ
    furniture  (AI) identify furniture blocks the rules could not
    check      guardrail findings: furniture conflicts, interior windows, rooms without an entrance...
    review     (AI) patches for suspects + one decision per finding
    fix        apply them, then enforce the furniture rule (guardrails.resolve)

The AI steps run when asked (uploads) and an OpenAI key is configured. Any AI failure falls back to the
rule-based result and the findings' defaults: the pipeline never fails because of the LLM, and the hard
rules hold either way. CPU-heavy steps run in worker processes (workers.py).
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config import Settings
from app.schemas.checks import CheckReport, ClassificationSummary
from app.schemas.parse import ParseOptions
from app.services import guardrails, reviewer, workers
from app.services.drawing_reader import DrawingReadError
from app.services.dxf_parser import ParseError, ParseResult
from app.services.furniture_llm import classify_unknown_furniture
from app.services.furniture_placement import place_furniture
from app.services.layers import layer_patterns
from app.services.llm import LLMUnavailable
from app.services.llm_classify import propose_classification

logger = logging.getLogger(__name__)

Stage = Callable[[str], Awaitable[None]]
STAGES = ("queued", "converting", "reading", "classifying", "rebuilding", "furniture", "checking", "reviewing",
          "fixing", "saving")  # fmt: skip
MIN_WALL_SHARE = 0.3  # the AI's layer roles are rejected if they keep fewer walls than this share of the rules'


@dataclass
class Built:
    drawing: dict | None
    result: ParseResult | None  # None when the drawing could not be turned into a building
    error: ParseError | None
    options: ParseOptions  # the layer/block roles actually used
    report: CheckReport


async def _no_stage(_: str) -> None:
    return None


async def _read(dxf: bytes, options: ParseOptions, settings: Settings) -> tuple[dict, ParseResult | ParseError]:
    try:
        return await workers.read_drawing(dxf, layer_patterns(settings, options), options, settings)
    except DrawingReadError as exc:
        raise ParseError(str(exc)) from exc


def _walls(outcome: ParseResult | ParseError) -> int:
    return len(outcome.building.walls) if isinstance(outcome, ParseResult) else 0


async def build_model(
    dxf: bytes, options: ParseOptions | None, settings: Settings, *, ai: bool, stage: Stage = _no_stage
) -> Built:
    """`options=None` means a fresh drawing: the AI may choose the layer roles. Raises ParseError only
    when the file cannot be read at all; a drawing that yields no building comes back with `error`."""
    use_ai = ai and bool(settings.openai_api_key)
    report = CheckReport(ai="used" if use_ai else "unavailable" if ai else "off")
    if ai and not use_ai:
        report.notes.append("No OpenAI key on the server: the automatic checks used their default fixes.")
    ai_failures = 0

    def failed(step: str, exc: Exception) -> None:
        nonlocal ai_failures
        ai_failures += 1
        if isinstance(exc, LLMUnavailable):
            logger.warning("AI %s failed: %s", step, exc)
        else:
            logger.exception("AI %s failed unexpectedly", step)
        report.notes.append(f"AI {step} unavailable ({exc}); used the rule-based result.")

    effective = options or ParseOptions()
    await stage("reading")
    drawing, outcome = await _read(dxf, effective, settings)

    if use_ai and options is None:
        await stage("classifying")
        try:
            proposal = await propose_classification(drawing, settings)
        except Exception as exc:  # noqa: BLE001 - any AI failure falls back to the rules
            failed("layer classification", exc)
        else:
            summary = ClassificationSummary(
                applied=False, layers=len(proposal.layers), blocks=len(proposal.blocks), notes=proposal.notes
            )
            proposed = proposal.parse_options
            if proposed.model_dump(exclude_none=True) and proposed != effective:
                await stage("rebuilding")
                drawing_ai, outcome_ai = await _read(dxf, proposed, settings)
                rules, by_ai = _walls(outcome), _walls(outcome_ai)
                if by_ai and by_ai >= MIN_WALL_SHARE * rules:
                    drawing, outcome, effective = drawing_ai, outcome_ai, proposed
                    summary.applied = True
                else:
                    report.notes.append(
                        f"The AI's layer roles gave {by_ai} walls against {rules} from the rules; kept the rules."
                    )
            report.classification = summary

    if isinstance(outcome, ParseError):
        return Built(drawing, None, outcome, effective, report)
    result = outcome

    if use_ai and result.unresolved_furniture:
        await stage("furniture")
        try:
            await identify_furniture(result, settings)
        except Exception as exc:  # noqa: BLE001
            failed("furniture identification", exc)

    await stage("checking")
    analysis = await workers.run(settings, guardrails.analyze, result.building)
    report.notes += analysis.notes
    patches, decisions = [], {}
    if use_ai:
        await stage("reviewing")
        try:
            review = await reviewer.ai_review(
                result.building, result.warnings, result.stats, analysis.findings, settings
            )
        except Exception as exc:  # noqa: BLE001
            failed("review", exc)
        else:
            patches, decisions = review.patches, review.decisions
            report.notes += [f"Ignored AI answer: {r}" for r in review.rejected]
            if review.notes:
                report.notes.append(f"AI review: {review.notes}")
    if use_ai and ai_failures and ai_failures >= 2:
        report.ai = "unavailable"

    await stage("fixing")
    resolution = await workers.run(settings, guardrails.resolve, result.building, analysis.findings, patches, decisions)
    result.building = resolution.building
    report.findings, report.changes = resolution.findings, resolution.changes
    report.notes += resolution.notes
    result.stats["furnitureCount"] = len(result.building.furniture)
    result.stats["wallCount"] = len(result.building.walls)
    result.stats["checks"] = {"findings": len(report.findings), "changes": len(report.changes), "ai": report.ai}
    return Built(drawing, result, None, effective, report)


async def identify_furniture(result: ParseResult, settings: Settings) -> None:
    """Ask the LLM about furniture blocks the rules could not identify, then place them by rule."""
    unresolved = result.unresolved_furniture
    if not unresolved or not settings.openai_api_key:
        return
    categories = await classify_unknown_furniture(unresolved, result.building.rooms, settings)
    identified = []
    for index, category in categories.items():
        if category != "none":
            candidate = unresolved[index]
            candidate.category, candidate.matched_by = category, "llm"
            identified.append(candidate)
    if not identified:
        return
    start = len(result.building.furniture) + 1
    added = place_furniture(identified, result.building.walls, result.warnings, start_index=start)
    result.building.furniture.extend(added)
    result.stats["furnitureCount"] = len(result.building.furniture)
    result.stats["llmFurniture"] = [f.source_block for f in added]
    result.stats["unresolvedFurnitureBlocks"] = sorted({c.block for c in unresolved if c.matched_by != "llm"})
