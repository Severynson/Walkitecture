"""Application settings, loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://walkitecture:walkitecture@localhost:5433/walkitecture"
    cors_origins: CsvList = ["http://localhost:5173", "http://localhost:3000"]
    max_upload_mb: int = 25
    log_level: str = "INFO"

    # --- DWG -> DXF conversion (LibreDWG) ---------------------------------------------
    dwg2dxf_path: str = "dwg2dxf"
    dwg_convert_timeout_s: float = 90.0

    # --- OpenAI edit interpretation ----------------------------------------------------
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_s: float = 30.0

    # --- Drawing convention (layer name patterns, case-insensitive, glob syntax) -------
    wall_layers: CsvList = ["WALL", "WALLS", "A-WALL*"]
    window_layers: CsvList = ["WINDOW", "WINDOWS", "A-GLAZ*", "A-WIND*"]
    door_layers: CsvList = ["DOOR", "DOORS", "A-DOOR*"]
    room_layers: CsvList = ["ROOM", "ROOMS", "A-AREA*", "A-ROOM*"]

    # --- Geometry defaults (meters) -----------------------------------------------------
    default_wall_thickness: float = 0.15
    default_ceiling_height: float = 2.7
    default_window_height: float = 1.2
    default_window_sill_height: float = 0.9
    default_door_height: float = 2.1

    @field_validator("cors_origins", "wall_layers", "window_layers", "door_layers", "room_layers", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
