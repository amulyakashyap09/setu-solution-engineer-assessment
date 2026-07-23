"""A single error envelope for every failure mode.

Clients integrating with a payments API should not have to branch on
whether a failure came from FastAPI's validation layer, an explicit
HTTPException, or an unhandled crash. All three are rendered as:

    {"error": {"code": "...", "message": "...", "details": [...]}}
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("recon")

_STATUS_CODES = {
    400: "bad_request",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    500: "internal_error",
}


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, details=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def error_response(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    payload: dict = {"error": {"code": code, "message": message}}
    if details is not None:
        payload["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=payload)


def register_error_handlers(app: FastAPI) -> None:

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())),
                "message": err.get("msg", ""),
                "type": err.get("type", ""),
            }
            for err in exc.errors()
        ]
        return error_response(
            422, "validation_error", "Request validation failed", details
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException):
        code = _STATUS_CODES.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        # Log the detail, return a generic message: internal errors in a
        # payments service should never leak SQL or stack frames.
        logger.exception("unhandled error: %s", exc)
        return error_response(
            500, "internal_error", "An unexpected error occurred"
        )
