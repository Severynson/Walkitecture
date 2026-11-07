"""Serve the built frontend (Vite `dist/`) from the same origin as the API (production image).

* Real files (``/assets/*.js``, ``/furniture/*.glb``, ``/favicon.ico``...) are returned as-is.
* Any other GET that is not an API/docs route returns ``index.html`` (client-side routing).
* Unknown ``/api/*`` paths keep returning the API's JSON 404 instead of the SPA.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api.errors import ApiError

# Hashed build output never changes under the same name; everything else must be revalidated.
_IMMUTABLE = "public, max-age=31536000, immutable"
_REVALIDATE = "no-cache"


def mount_spa(app: FastAPI, static_dir: Path) -> None:
    root = static_dir.resolve()
    index = root / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise ApiError(404, "not_found", f"No API route for /{path}.")
        if path:
            candidate = (root / path).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                cache = _IMMUTABLE if candidate.parent.name == "assets" else _REVALIDATE
                return FileResponse(candidate, headers={"Cache-Control": cache})
        return FileResponse(index, headers={"Cache-Control": _REVALIDATE})


def spa_available(static_dir: str | None) -> Path | None:
    if not static_dir:
        return None
    path = Path(static_dir)
    return path if (path / "index.html").is_file() else None
