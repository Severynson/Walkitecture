"""Application settings, loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]
FOOT = 0.3048  # meters


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://walkitecture:walkitecture@localhost:5433/walkitecture"
    cors_origins: CsvList = ["http://localhost:5173", "http://localhost:3000"]
    max_upload_mb: int = 25
    log_level: str = "INFO"

    # --- Auth (JWT) ---------------------------------------------------------------------
    jwt_secret: str | None = None  # unset -> random per process (tokens die on restart)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7

    # --- DWG -> DXF conversion (LibreDWG) ---------------------------------------------
    dwg2dxf_path: str = "dwg2dxf"
    dwg_convert_timeout_s: float = 90.0

    # --- OpenAI edit interpretation ----------------------------------------------------
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    openai_timeout_s: float = 30.0

    # --- Drawing convention (layer name patterns, case-insensitive, glob syntax) -------
    # English (AIA) + Spanish defaults; the example drawings in /examples are Spanish.
    wall_layers: CsvList = ["WALL", "WALLS", "A-WALL*", "MURO*", "MUROS*"]
    window_layers: CsvList = ["WINDOW", "WINDOWS", "A-GLAZ*", "A-WIND*", "VENTANA*"]
    door_layers: CsvList = ["DOOR", "DOORS", "A-DOOR*", "PUERTA*"]
    # Mixed door + window layers: blocks with a swing arc are doors, others windows.
    opening_layers: CsvList = ["CARPINTERIA*", "CARPENTRY*", "A-OPEN*", "CERR*"]
    room_layers: CsvList = ["ROOM", "ROOMS", "A-AREA*", "A-ROOM*", "SUPERFICIE*"]
    furniture_layers: CsvList = [
        "FURN*", "A-FURN*", "I-FURN*", "A-FLOR-FIXT*", "PLUMB*", "MUEBLE*", "MOB*", "SANITARIO*",
    ]  # fmt: skip

    # --- Geometry defaults (meters; ARCHITECTURE.md specifies them in feet) -------------
    default_wall_thickness: float = 0.15
    default_ceiling_height: float = 10 * FOOT  # all walls 10 ft
    default_window_sill_height: float = 2.5 * FOOT  # windows span 2.5 ft ...
    default_window_height: float = (6.5 - 2.5) * FOOT  # ... to 6.5 ft
    default_door_height: float = 7 * FOOT  # door frames 0 to 7 ft

    @field_validator(
        "cors_origins",
        "wall_layers",
        "window_layers",
        "door_layers",
        "opening_layers",
        "room_layers",
        "furniture_layers",
        mode="before",
    )
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
