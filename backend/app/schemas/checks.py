"""Report of the automatic checks that run before a plan's 3D scene is built (ARCHITECTURE.md §4.4)."""

from typing import Any, Literal

from pydantic import Field

from app.schemas.base import CamelModel

FindingKind = Literal[
    "furniture_conflict", "furniture_outside", "interior_window", "enclosed_space", "no_exterior_door", "facade_gap"
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


class CheckReport(CamelModel):
    ai: Literal["used", "unavailable", "off"]
    classification: ClassificationSummary | None = None
    findings: list[CheckFinding] = Field(default_factory=list)
    changes: list[CheckChange] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
