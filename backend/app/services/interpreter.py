"""Turn a natural-language request into an allowlisted edit command (data, never code).

OpenAI is used when OPENAI_API_KEY is configured; otherwise (or if the call fails) a deterministic
rule-based parser handles common phrasings, so the demo never depends on the LLM being reachable.
"""

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Literal

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas.building import Building, Point2
from app.schemas.edits import EditCommand, EditCommandAdapter
from app.services.geometry import dot, left_normal
from app.services.llm import completion_options, make_client

logger = logging.getLogger(__name__)

Source = Literal["openai", "rules"]


@dataclass
class Interpretation:
    command: EditCommand | None
    source: Source
    explanation: str
    clarification: str | None = None
    notes: list[str] = field(default_factory=list)


# --- Shared helpers ------------------------------------------------------------------------

_LENGTH_UNITS = {
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001, "millimetre": 0.001, "millimetres": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01, "centimetre": 0.01, "centimetres": 0.01,
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048, "'": 0.3048,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254, '"': 0.0254,
}  # fmt: skip
_LENGTH_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(millimet(?:er|re)s?|centimet(?:er|re)s?|met(?:er|re)s?|mm|cm|m|feet|foot|ft|inch(?:es)?|in|'|\")"
    r"(?![a-z])",
    re.IGNORECASE,
)
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", re.IGNORECASE)
_FACTOR_WORDS = {"twice": 2.0, "double": 2.0, "doubled": 2.0, "triple": 3.0, "half": 0.5, "halve": 0.5}
_TOWARD_WORDS = ("forward", "away", "further", "farther", "ahead")
_BACK_WORDS = ("back", "backward", "backwards", "toward me", "towards me", "closer")


def _lengths(text: str) -> list[float]:
    return [float(value) * _LENGTH_UNITS[unit.lower()] for value, unit in _LENGTH_RE.findall(text)]


def _context(building: Building, selected_id: str | None) -> dict:
    """Compact model summary for the LLM: IDs and key dimensions, not full geometry."""
    walls = []
    for w in building.walls:
        walls.append(
            {
                "id": w.id,
                "length": round(w.length, 2),
                "height": w.height,
                "openings": [
                    {"id": o.id, "type": o.type, "width": o.width, "height": o.height, "sillHeight": o.sill_height}
                    for o in w.openings
                ],
            }
        )
    return {
        "ceilingHeight": building.ceiling_height,
        "selectedId": selected_id,
        "selectedKind": building.kind_of(selected_id) if selected_id else None,
        "walls": walls,
        "rooms": [{"id": r.id, "name": r.name} for r in building.rooms],
    }


def _direction_sign(building: Building, wall_id: str, camera_forward: Point2 | None, text: str) -> float | None:
    """+1/-1 to map 'forward/back' onto the wall's normal using the camera direction; None if unknown."""
    lowered = text.lower()
    toward = any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in _TOWARD_WORDS)
    back = any(re.search(rf"\b{re.escape(w)}\b", lowered) for w in _BACK_WORDS)
    if not (toward or back):
        return 1.0
    wall = building.find_wall(wall_id)
    if wall is None or camera_forward is None or math.hypot(*camera_forward) < 1e-6:
        return None
    alignment = dot(left_normal(wall.start, wall.end), camera_forward)
    if abs(alignment) < 0.2:
        return None  # looking along the wall: "forward" is ambiguous
    sign = 1.0 if alignment > 0 else -1.0
    return sign if toward else -sign


# --- Rule-based interpreter -------------------------------------------------------------------


def interpret_with_rules(
    text: str, selected_id: str | None, building: Building, camera_forward: Point2 | None = None
) -> Interpretation:
    lowered = text.lower()
    selected_kind = building.kind_of(selected_id) if selected_id else None

    def clarify(message: str) -> Interpretation:
        return Interpretation(
            command=None, source="rules", explanation="Could not map the request.", clarification=message
        )

    if "ceiling" in lowered:
        lengths = [v for v in _lengths(text) if v > 0]
        if not lengths:
            return clarify("What ceiling height do you want? For example: 'Set the ceiling to 3 meters.'")
        value = lengths[0]
        if re.search(r"\bby\b", lowered):
            delta = -value if re.search(r"\b(lower|reduce|decrease|drop)\b", lowered) else value
            value = building.ceiling_height + delta
        command = {"operation": "set_ceiling_height", "parameters": {"height": round(value, 4)}}
        return _finish(command, f"Set the ceiling height to {value:.2f} m.")

    if "window" in lowered or selected_kind == "window":
        factor = _width_factor(lowered)
        lengths = [v for v in _lengths(text) if v > 0]
        all_windows = bool(re.search(r"\b(all|every|each)\b.*\bwindows?\b|\bwindows\b", lowered))
        if all_windows and selected_kind != "window":
            if factor is None:
                return clarify("By how much should all windows change? For example: 'Make all windows 20% wider.'")
            command = {"operation": "resize_windows", "parameters": {"factor": factor}}
            return _finish(command, f"Scale the width of every window by {factor:g}.")
        if selected_kind not in ("window", "wall"):
            return clarify("Click the window you want to change first, then repeat the request.")
        params: dict[str, float] = {}
        if factor is not None:
            params["factor"] = factor
        elif lengths and re.search(r"\b(wide|width|across)\b", lowered):
            params["width"] = lengths[0]
        elif lengths and re.search(r"\b(tall|height|high)\b", lowered):
            params["height"] = lengths[0]
        if not params:
            return clarify("How should the window change? For example: '50% wider' or '1.5 m wide'.")
        command = {"operation": "resize_selected_window", "targetId": selected_id, "parameters": params}
        return _finish(command, f"Resize window {selected_id}: {params}.")

    if re.search(r"\b(move|shift|push|pull|slide)\b", lowered):
        if selected_kind != "wall":
            return clarify("Click the wall you want to move first.")
        lengths = _lengths(text)
        if not lengths:
            return clarify("How far should the wall move? For example: 'Move this wall back 30 cm.'")
        sign = _direction_sign(building, selected_id, camera_forward, text)
        if sign is None:
            return clarify("Which way should the wall move? Face the wall and say 'forward' or 'back'.")
        offset = round(sign * lengths[0], 4)
        command = {"operation": "move_selected_wall", "targetId": selected_id, "parameters": {"offset": offset}}
        return _finish(command, f"Move wall {selected_id} by {offset:+.2f} m along its normal.")

    return clarify(
        "Supported edits: resize the selected window, resize all windows, set the ceiling height, "
        "or move a selected free-standing wall."
    )


def _width_factor(lowered: str) -> float | None:
    shrink = bool(re.search(r"\b(narrower|smaller|shrink|reduce|decrease|thinner)\b", lowered))
    if m := _PERCENT_RE.search(lowered):
        pct = float(m.group(1)) / 100
        return round(1 - pct if shrink else 1 + pct, 4)
    for word, factor in _FACTOR_WORDS.items():
        if re.search(rf"\b{word}\b", lowered):
            return factor
    if m := re.search(r"(\d+(?:\.\d+)?)\s*(?:x|times)\b", lowered):
        return float(m.group(1))
    return None


def _finish(raw: dict, explanation: str) -> Interpretation:
    try:
        command = EditCommandAdapter.validate_python(raw)
    except ValidationError as exc:
        return Interpretation(
            command=None,
            source="rules",
            explanation="The request mapped to an invalid command.",
            clarification=exc.errors()[0]["msg"],
        )
    return Interpretation(command=command, source="rules", explanation=explanation)


# --- OpenAI interpreter -----------------------------------------------------------------------


class _LLMEdit(BaseModel):
    """Flat schema for OpenAI structured outputs (strict mode handles flat nullable fields best)."""

    operation: Literal["resize_selected_window", "resize_windows", "set_ceiling_height", "move_selected_wall", "none"]
    target_id: str | None
    factor: float | None
    width: float | None
    height: float | None
    offset: float | None
    explanation: str
    clarification: str | None


SYSTEM_PROMPT = """You translate a user's request about an architectural floor plan into ONE edit command.
You never write code. You only choose from these operations (all lengths in meters):

- resize_selected_window: target_id = the selected window ID. Use `factor` for relative width changes
  ("50% wider" -> 1.5, "20% narrower" -> 0.8), or `width` for an absolute width. `height` sets the
  window height. Set unused fields to null.
- resize_windows: every window. `factor` only. target_id = null.
- set_ceiling_height: `height` = new absolute ceiling height (convert "raise by 30 cm" using the
  current ceilingHeight). target_id = null.
- move_selected_wall: target_id = the selected wall ID. `offset` = signed distance along the wall's
  left-hand normal. If the user says forward/back, use cameraForwardDotNormal from the context:
  forward means sign(cameraForwardDotNormal), back means the opposite. If that value is null or
  near 0 and a direction word is used, ask for clarification instead.
- none: the request is ambiguous, unsupported, or refers to an object that is not selected. Put a
  short question or explanation for the user in `clarification`.

Rules: only use IDs that appear in the context. "This"/"the selected" refers to selectedId. Convert
cm/mm/feet/inches to meters. `explanation` is one short sentence describing the edit."""


async def interpret_with_openai(
    text: str,
    selected_id: str | None,
    building: Building,
    camera_forward: Point2 | None,
    settings: Settings,
    client: AsyncOpenAI | None = None,
) -> Interpretation:
    context = _context(building, selected_id)
    wall = building.find_wall(selected_id) if selected_id else None
    if wall and camera_forward:
        context["cameraForwardDotNormal"] = round(dot(left_normal(wall.start, wall.end), camera_forward), 3)
    else:
        context["cameraForwardDotNormal"] = None

    client = client or make_client(settings)
    completion = await client.chat.completions.parse(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Context:\n{json.dumps(context)}\n\nRequest: {text}"},
        ],
        response_format=_LLMEdit,
        **completion_options(settings),
    )
    message = completion.choices[0].message
    if message.refusal or message.parsed is None:
        return Interpretation(
            command=None, source="openai", explanation="The model declined the request.",
            clarification=message.refusal or "Please rephrase the request.",
        )  # fmt: skip
    return _from_llm(message.parsed)


def _from_llm(out: _LLMEdit) -> Interpretation:
    if out.operation == "none":
        return Interpretation(
            command=None,
            source="openai",
            explanation=out.explanation,
            clarification=out.clarification or "Please rephrase the request.",
        )
    params: dict[str, float] = {}
    if out.operation == "resize_selected_window":
        params = {
            k: v for k, v in (("factor", out.factor), ("width", out.width), ("height", out.height)) if v is not None
        }
    elif out.operation == "resize_windows":
        params = {"factor": out.factor} if out.factor is not None else {}
    elif out.operation == "set_ceiling_height":
        params = {"height": out.height} if out.height is not None else {}
    elif out.operation == "move_selected_wall":
        params = {"offset": out.offset} if out.offset is not None else {}

    raw: dict = {"operation": out.operation, "parameters": params}
    if out.operation in ("resize_selected_window", "move_selected_wall"):
        raw["targetId"] = out.target_id
    try:
        command = EditCommandAdapter.validate_python(raw)
    except ValidationError as exc:
        return Interpretation(
            command=None,
            source="openai",
            explanation=out.explanation,
            clarification=f"The model produced an invalid command: {exc.errors()[0]['msg']}",
        )
    return Interpretation(command=command, source="openai", explanation=out.explanation)


# --- Entry point ----------------------------------------------------------------------------------


async def interpret_edit(
    text: str,
    selected_id: str | None,
    building: Building,
    settings: Settings,
    camera_forward: Point2 | None = None,
) -> Interpretation:
    if settings.openai_api_key:
        try:
            return await interpret_with_openai(text, selected_id, building, camera_forward, settings)
        except (OpenAIError, ValidationError, ValueError) as exc:
            logger.warning("OpenAI interpretation failed, falling back to rules: %s", exc)
            result = interpret_with_rules(text, selected_id, building, camera_forward)
            result.notes.append(f"OpenAI unavailable ({type(exc).__name__}); used the rule-based interpreter.")
            return result
    return interpret_with_rules(text, selected_id, building, camera_forward)
