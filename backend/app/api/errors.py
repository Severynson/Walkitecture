"""Uniform error responses: {"error": {"code", "message", "problems"}}."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.services.dwg_convert import ConversionError
from app.services.dxf_parser import ParseError
from app.services.edits import EditRejected
from app.services.geometry import Problem
from app.services.plan_store import NothingToUndo, PlanNotFound, PlanNotParsed, VersionConflict


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, problems: list[Problem] | None = None):
        self.status, self.code, self.message, self.problems = status, code, message, problems or []
        super().__init__(message)


def _response(status: int, code: str, message: str, problems: list[Problem] | None = None) -> JSONResponse:
    body = {
        "error": {
            "code": code,
            "message": message,
            "problems": [p.model_dump(by_alias=True) for p in problems or []],
        }
    }
    return JSONResponse(status_code=status, content=body)


# exception type -> (HTTP status, error code)
_SIMPLE_ERRORS: dict[type[Exception], tuple[int, str]] = {
    PlanNotFound: (404, "plan_not_found"),
    PlanNotParsed: (409, "plan_not_parsed"),
    VersionConflict: (409, "version_conflict"),
    NothingToUndo: (409, "nothing_to_undo"),
    ParseError: (422, "parse_failed"),
    ConversionError: (422, "dwg_conversion_failed"),
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _response(exc.status, exc.code, exc.message, exc.problems)

    @app.exception_handler(EditRejected)
    async def _edit_rejected(_: Request, exc: EditRejected) -> JSONResponse:
        return _response(422, "edit_rejected", str(exc), exc.problems)

    for exc_type, (status, code) in _SIMPLE_ERRORS.items():

        async def _handler(_: Request, exc: Exception, status: int = status, code: str = code) -> JSONResponse:
            return _response(status, code, str(exc))

        app.add_exception_handler(exc_type, _handler)
