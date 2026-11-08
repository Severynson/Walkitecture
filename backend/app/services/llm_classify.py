"""LLM layer/block classification (ARCHITECTURE.md §4.2).

The LLM sees only the drawing *inventory* (layers, blocks, counts, rule categories, sample texts), never
raw entities, and answers with roles plus confidences. Code validates every name against the drawing,
drops low-confidence items, and turns the rest into ordinary `ParseOptions` (layer lists +
`blockOverrides`), so the deterministic pipeline does the actual geometry.
"""

import glob
import json
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from app.config import Settings
from app.schemas.base import CamelModel
from app.schemas.parse import FURNITURE_CATEGORIES, BlockRole, ParseOptions
from app.services.llm import structured

LayerRole = Literal["wall", "window", "door", "opening", "room", "furniture", "ignore"]
MAX_LAYERS = 150
MAX_BLOCKS = 150
MAX_TEXTS = 25


class _Layer(BaseModel):
    layer: str
    role: LayerRole
    confidence: float


class _Block(BaseModel):
    block: str
    role: BlockRole
    confidence: float


class _Answer(BaseModel):
    layers: list[_Layer]
    blocks: list[_Block]
    notes: str


class LayerProposal(CamelModel):
    layer: str
    role: LayerRole
    confidence: float
    current: dict[str, int] = Field(description="Categories the rules assigned to this layer's entities")


class BlockProposal(CamelModel):
    block: str
    role: BlockRole
    confidence: float
    current: str = Field(description="Category the rules assigned to this block")


class ClassificationProposal(CamelModel):
    layers: list[LayerProposal]
    blocks: list[BlockProposal]
    notes: str
    dropped: list[str] = Field(description="Proposals ignored: unknown names or confidence below the threshold")
    parse_options: ParseOptions = Field(description="Ready for POST /parse (or ?apply=true)")


PROMPT = (
    """You classify the layers and blocks of an architectural floor-plan drawing (DWG/DXF, often
non-English, e.g. Spanish MURO=wall, VENTANA=window, PUERTA=door, CARPINTERIA=doors+windows,
MUEBLES=furniture, SUPERFICIE=room areas). For each layer choose its role:
wall (wall outlines), window, door, opening (mixed doors and windows), room (room outlines or room name
labels), furniture, or ignore (dimensions, hatching, text, electrical, axes, plaster/finish lines, other
sheets). For each block choose: door, window, """
    + ", ".join(FURNITURE_CATEGORIES)
    + """, or ignore
(washbasins, bidets, stoves, washing machines, plants, symbols, arrows). "sink" is a kitchen sink counter,
"kitchen" a kitchen counter/cabinet unit, "cabinet" a low or filing cabinet, "dresser" a chest of drawers.
Use names, entity types, counts, sizes (drawing units) and the categories the rules already assigned.
Give a confidence from 0 to 1. Only list layers and blocks that appear in the input; include every layer
that contains geometry."""
)


def inventory(drawing: dict[str, Any]) -> dict[str, Any]:
    """The compact, LLM-sized view of a drawing JSON."""
    texts: list[str] = []
    for entity in drawing.get("entities", []):
        if entity.get("type") in ("TEXT", "MTEXT"):
            text = " ".join(str(entity["geometry"].get("text", "")).split())
            if text and text not in texts:
                texts.append(text)
        if len(texts) >= MAX_TEXTS:
            break
    return {
        "units": drawing.get("units"),
        "extents": drawing.get("extents"),
        "summary": drawing.get("summary"),
        "layers": drawing.get("layers", [])[:MAX_LAYERS],
        "blocks": [
            {k: b.get(k) for k in ("name", "insertCount", "category", "entityTypes", "size")}
            for b in drawing.get("blocks", [])[:MAX_BLOCKS]
        ],
        "sampleTexts": texts,
    }


async def propose_classification(
    drawing: dict[str, Any], settings: Settings, *, min_confidence: float = 0.5, client: AsyncOpenAI | None = None
) -> ClassificationProposal:
    view = inventory(drawing)
    answer = await structured(settings, PROMPT, json.dumps(view, ensure_ascii=False), _Answer, client)

    layer_info = {layer["name"]: layer for layer in drawing.get("layers", [])}
    block_info = {block["name"]: block for block in drawing.get("blocks", [])}
    layers: list[LayerProposal] = []
    blocks: list[BlockProposal] = []
    dropped: list[str] = []
    for item in answer.layers:
        if item.layer not in layer_info:
            dropped.append(f"layer {item.layer!r}: not in the drawing")
        elif item.confidence < min_confidence:
            dropped.append(f"layer {item.layer!r}: confidence {item.confidence:.2f}")
        else:
            current = layer_info[item.layer].get("categories", {})
            layers.append(LayerProposal(layer=item.layer, role=item.role, confidence=item.confidence, current=current))
    for item in answer.blocks:
        if item.block not in block_info:
            dropped.append(f"block {item.block!r}: not in the drawing")
        elif item.confidence < min_confidence:
            dropped.append(f"block {item.block!r}: confidence {item.confidence:.2f}")
        else:
            current = block_info[item.block].get("category", "other")
            blocks.append(BlockProposal(block=item.block, role=item.role, confidence=item.confidence, current=current))

    return ClassificationProposal(
        layers=layers,
        blocks=blocks,
        notes=answer.notes,
        dropped=dropped,
        parse_options=to_parse_options(layers, blocks),
    )


def to_parse_options(layers: list[LayerProposal], blocks: list[BlockProposal]) -> ParseOptions:
    """Layer roles -> exact-match layer lists (roles nobody claimed keep the server defaults)."""
    by_role: dict[str, list[str]] = {}
    for proposal in layers:
        if proposal.role != "ignore":
            by_role.setdefault(proposal.role, []).append(glob.escape(proposal.layer))
    return ParseOptions(
        wall_layers=by_role.get("wall"),
        window_layers=by_role.get("window"),
        door_layers=by_role.get("door"),
        opening_layers=by_role.get("opening"),
        room_layers=by_role.get("room"),
        furniture_layers=by_role.get("furniture"),
        block_overrides={b.block: b.role for b in blocks} or None,
    )
