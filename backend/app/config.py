"""Application settings, loaded from environment variables (and an optional .env file)."""

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]
FOOT = 0.3048  # meters
DEFAULT_SEED_PASSWORD = "walkitecture-demo"  # docker-compose dev default; refused in production


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Infrastructure -------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://walkitecture:walkitecture@localhost:5433/walkitecture"
    cors_origins: CsvList = ["http://localhost:5173", "http://localhost:3000"]
    max_upload_mb: int = 25
    log_level: str = "INFO"
    # Built frontend (Vite dist/) to serve at "/"; set in the Docker image. Unset -> "/" redirects to /docs.
    static_dir: str | None = None

    # production: refuse to start without a strong JWT_SECRET or with the default seed credentials
    app_env: Literal["development", "production"] = "development"

    # --- Auth (JWT) ---------------------------------------------------------------------
    jwt_secret: str | None = None  # unset -> random per process (tokens die on restart)
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60 * 24 * 7
    # Attempts per minute per client IP (0 disables); behind Traefik set FORWARDED_ALLOW_IPS so the IP is real
    login_rate_limit: int = 10
    register_rate_limit: int = 5

    # --- Seed (scripts/seed.py, run on container start) ---------------------------------
    # Seeding is off unless both are set (docker-compose sets dev defaults).
    seed_username: str | None = None
    seed_password: str | None = None
    seed_sample_plans: bool = True  # give the seed user the demo apartment + /examples drawings
    seed_examples_dir: str | None = None  # default: <repo>/examples when present

    # --- DWG -> DXF conversion (LibreDWG) ---------------------------------------------
    dwg2dxf_path: str = "dwg2dxf"
    dwg_convert_timeout_s: float = 90.0

    # --- OpenAI edit interpretation ----------------------------------------------------
    openai_api_key: str | None = None
    openai_model: str = "gpt-5-mini"
    # Reasoning models (gpt-5*) think before answering: at the default "medium" effort the layer
    # classification of a real drawing took ~35 s; "low" ~22 s, "minimal" ~15 s, same answers.
    # Empty = the model's default (also use empty for non-reasoning models such as gpt-4o).
    openai_reasoning_effort: Literal["", "minimal", "low", "medium", "high"] = "low"
    openai_timeout_s: float = 120.0  # per attempt; the whole-plan steps (classify/review) are the slow ones
    openai_max_retries: int = 1

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

    def production_problems(self) -> list[str]:
        """Reasons this configuration must not run with APP_ENV=production (empty = fine)."""
        if self.app_env != "production":
            return []
        problems = []
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be set to at least 32 characters (openssl rand -hex 32).")
        if self.seed_username and self.seed_password == DEFAULT_SEED_PASSWORD:
            problems.append("The default seed password is not allowed; set SEED_USERNAME= (empty) to disable seeding.")
        return problems


@lru_cache
def get_settings() -> Settings:
    return Settings()
