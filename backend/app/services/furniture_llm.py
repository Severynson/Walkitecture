"""LLM fallback for furniture blocks the rules could not identify (ARCHITECTURE.md: "if this approach
doesn't work, it is worth implementing furniture placement with an LLM API").

Only the *category* is asked of the model; placement and orientation stay rule-based, so the
geometry remains deterministic. Used only when OPENAI_API_KEY is configured.
"""

import json
import logging
from typing import Literal

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.schemas.building import Room
from app.schemas.parse import FURNITURE_CATEGORIES
from app.services.furniture_placement import FurnitureCandidate
from app.services.geometry import point_in_polygon
from app.services.llm import completion_options, make_client

logger = logging.getLogger(__name__)

Category = Literal[(*FURNITURE_CATEGORIES, "none")]  # type: ignore[valid-type]


class _Item(BaseModel):
    index: int
    category: Category


class _Answer(BaseModel):
    items: list[_Item]


PROMPT = f"""You identify furniture in architectural CAD drawings. For each block below, choose the
category of the 3D model that should represent it: {", ".join(FURNITURE_CATEGORIES)}, or none
(washbasins, bidets, stoves, washing machines, plants, TVs, symbols, or anything not in the list).
"sink" is a kitchen sink counter, "kitchen" a kitchen counter or cabinet unit, "cabinet" a low cabinet or a
filing cabinet, "dresser" a chest of drawers. Block names may be Spanish, abbreviated, or catalog numbers;
use the layer, footprint size (meters) and the room it stands in. Answer for every index."""


def _room_of(candidate: FurnitureCandidate, rooms: list[Room]) -> str | None:
    for room in rooms:
        if room.polygon and point_in_polygon(candidate.center, room.polygon):
            return room.name
    labelled = [r for r in rooms if r.label_position]
    if not labelled:
        return None
    cx, cy = candidate.center
    nearest = min(labelled, key=lambda r: (r.label_position[0] - cx) ** 2 + (r.label_position[1] - cy) ** 2)
    return nearest.name


async def classify_unknown_furniture(
    candidates: list[FurnitureCandidate], rooms: list[Room], settings: Settings, client: AsyncOpenAI | None = None
) -> dict[int, str]:
    """Return {candidate index: category} for candidates the model could identify."""
    if not candidates or not settings.openai_api_key:
        return {}
    blocks = [
        {
            "index": i,
            "block": c.block,
            "layer": c.layer,
            "footprint": [round(v, 2) for v in c.local_size],
            "room": _room_of(c, rooms),
        }
        for i, c in enumerate(candidates)
    ]
    client = client or make_client(settings)
    try:
        completion = await client.chat.completions.parse(
            model=settings.openai_model,
            messages=[{"role": "system", "content": PROMPT}, {"role": "user", "content": json.dumps(blocks)}],
            response_format=_Answer,
            **completion_options(settings),
        )
    except (OpenAIError, ValidationError, ValueError) as exc:
        logger.warning("LLM furniture classification failed: %s", exc)
        return {}
    answer = completion.choices[0].message.parsed
    if answer is None:
        return {}
    return {item.index: item.category for item in answer.items if 0 <= item.index < len(candidates)}
