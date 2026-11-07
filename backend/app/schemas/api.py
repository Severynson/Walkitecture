"""Request / response bodies for the HTTP API (camelCase on the wire)."""

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.base import CamelModel
from app.schemas.building import Building, Point2
from app.schemas.edits import EditCommand
from app.services.geometry import Problem


class ErrorBody(CamelModel):
    code: str
    message: str
    problems: list[Problem] = Field(default_factory=list)


class ErrorResponse(CamelModel):
    error: ErrorBody


class PlanSummary(CamelModel):
    id: uuid.UUID
    filename: str
    source_format: Literal["dwg", "dxf"]
    file_size: int
    status: Literal["uploaded", "parsed", "failed"]
    model_version: int
    has_model: bool
    error: str | None
    created_at: datetime
    updated_at: datetime


class PlanDetail(PlanSummary):
    parse_warnings: list[str]
    parse_stats: dict[str, Any] | None


class SceneResponse(CamelModel):
    plan_id: uuid.UUID
    version: int
    model: Building


class ParseResponse(CamelModel):
    plan: PlanDetail
    scene: SceneResponse


class EditRequest(CamelModel):
    command: EditCommand
    base_version: int | None = Field(
        default=None, description="Model version the client edited against; mismatches return 409."
    )
    source: Literal["api", "ui", "openai", "rules"] = "api"


class EditResponse(CamelModel):
    plan_id: uuid.UUID
    applied: bool
    version: int
    affected_ids: list[str]
    summary: str
    model: Building


class EditHistoryItem(CamelModel):
    id: int
    version: int
    command: dict[str, Any]
    affected_ids: list[str]
    summary: str
    source: str
    created_at: datetime


class ReplaceSceneRequest(CamelModel):
    model: Building
    base_version: int | None = None


class InterpretRequest(CamelModel):
    text: str = Field(min_length=1, max_length=500)
    selected_id: str | None = None
    plan_id: uuid.UUID | None = Field(default=None, description="Use the stored model of this plan as context.")
    model: Building | None = Field(default=None, description="Inline model context (when no planId is given).")
    camera_forward: Point2 | None = Field(
        default=None, description="Camera forward direction in plan coordinates; resolves 'forward'/'back'."
    )
    apply: bool = Field(default=False, description="Also apply the command to planId if it validates.")
    base_version: int | None = None


class EditPreview(CamelModel):
    affected_ids: list[str]
    summary: str


class InterpretResponse(CamelModel):
    command: EditCommand | None
    source: Literal["openai", "rules"]
    explanation: str
    clarification: str | None = None
    notes: list[str] = Field(default_factory=list)
    valid: bool | None = Field(default=None, description="Whether the command passes validation (null if unknown).")
    problems: list[Problem] = Field(default_factory=list)
    preview: EditPreview | None = None
    applied: EditResponse | None = None


class HealthResponse(CamelModel):
    status: Literal["ok", "degraded"]
    database: Literal["ok", "unavailable"]
    dwg_converter: bool
    interpreter: Literal["openai", "rules"]
