"""Report of the automatic checks that run before a plan's 3D scene is built (ARCHITECTURE.md §4.4)."""

from typing import Any, Literal

from pydantic import Field

from app.schemas.base import CamelModel

FindingKind = Literal[
    "furniture_conflict", "furniture_outside", "interior_window", "enclosed_space", "no_exterior_door", "facade_gap",
    "bathroom_open", "incompatible_furniture", "missing_wall"
]  # fmt: skip


class CheckOption(CamelModel):
    id: str
    description: str
    commands: list[dict[str, Any]] = Field(default_factory=list, description="Edit commands this option applies")
    remove_furniture: list[str] = Field(default_factory=list, description="Furniture this option removes")


class CheckFinding(CamelModel):
    id: str
    kind: FindingKind
    target_ids: list[str]
    detail: str
    options: list[CheckOption]
    default: str = Field(description="Option used when the AI is unavailable or gives no valid answer")
    decision: str | None = None
    decided_by: Literal["ai", "default"] | None = None
    reason: str | None = None


class CheckChange(CamelModel):
    target_ids: list[str]
    summary: str
    source: Literal["guardrails", "ai"]


class ClassificationSummary(CamelModel):
    applied: bool = Field(description="Whether the AI's layer/block roles were used to build the model")
    layers: int
    blocks: int
    notes: str


class RoomComparison(CamelModel):
    """The AI's visual check of one room: the original drawing next to the model built from it."""

    room: str
    drawing: str = Field(description="What the drawing shows there")
    model: str = Field(description="What the model has there")
    matches: bool
    note: str = ""


class CheckReport(CamelModel):
    ai: Literal["used", "unavailable", "off"]
    classification: ClassificationSummary | None = None
    findings: list[CheckFinding] = Field(default_factory=list)
    changes: list[CheckChange] = Field(default_factory=list)
    visual: list[RoomComparison] = Field(
        default_factory=list, description="Room-by-room comparison of the drawing's picture with the model's"
    )
    notes: list[str] = Field(default_factory=list)
