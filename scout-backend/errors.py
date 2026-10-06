# ==========================
# STRUCTURED ERROR RESPONSES
# ==========================
# Every route in main.py used to raise a plain HTTPException with a free-
# text `detail` string — fine for the frontend (which only ever reads
# `body.detail`, see app.js's api() helper) but inconsistent for any other
# consumer, since two similar failures (e.g. "not found" from two
# different endpoints) had no shared machine-readable shape.
#
# This module adds a stable `code` alongside the existing `detail` string,
# without changing `detail` itself or any existing route's status code or
# wording — so the frontend keeps working unmodified, while any consumer
# that wants a machine-readable shape gets one via `error.code`.
#
# AppError is opt-in: routes can keep raising fastapi.HTTPException exactly
# as before (still produces a valid, if code-less, structured response
# thanks to the handler below) or raise AppError for a codified one. New
# endpoints in this change use AppError; nothing existing was converted,
# per "modify only required" — this is purely additive.

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder


class ErrorCode:
    """Stable, machine-readable error codes. Kept as plain string
    constants (not an Enum) so new codes can be added without touching
    anything that already imports this module."""

    NOT_FOUND = "not_found"
    ALREADY_EXISTS = "already_exists"
    VALIDATION_ERROR = "validation_error"
    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    LIMIT_REACHED = "limit_reached"
    INTERNAL_ERROR = "internal_error"


class AppError(HTTPException):
    """An HTTPException that also carries a stable `code` and optional
    structured `details`, so it renders with the shared error shape below.
    Behaves exactly like HTTPException otherwise (same status_code/detail/
    headers), so anything that already catches HTTPException still catches
    this."""

    def __init__(self, status_code, detail, code=ErrorCode.BAD_REQUEST, details=None, headers=None):
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.code = code
        self.details = details


def _error_body(status_code, message, code, details=None):
    body = {
        # Kept for backward compatibility — the existing frontend (and
        # anything else already written against this API) reads
        # `body.detail` directly.
        "detail": message,
        "error": {
            "code": code,
            "message": message,
            "status": status_code,
        },
    }
    if details:
        body["error"]["details"] = details
    return body


def register_exception_handlers(app):
    """Wires the shared error shape onto the FastAPI app. Called once from
    main.py. Doesn't change status codes or messages for any existing
    route — only normalizes the JSON body shape."""

    @app.exception_handler(AppError)
    async def _handle_app_error(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.status_code, exc.detail, exc.code, exc.details),
            headers=exc.headers,
        )

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(request: Request, exc: HTTPException):
        # Plain (non-AppError) HTTPExceptions raised anywhere in the
        # existing codebase — mapped to the closest generic code by
        # status so the shape is still consistent even though the route
        # wasn't converted to AppError.
        code = {
            400: ErrorCode.BAD_REQUEST,
            401: ErrorCode.UNAUTHORIZED,
            404: ErrorCode.NOT_FOUND,
            409: ErrorCode.ALREADY_EXISTS,
            422: ErrorCode.VALIDATION_ERROR,
            429: ErrorCode.RATE_LIMITED,
            502: ErrorCode.UPSTREAM_ERROR,
        }.get(exc.status_code, ErrorCode.INTERNAL_ERROR)
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.status_code, str(exc.detail), code),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(request: Request, exc: RequestValidationError):
        # FastAPI/Pydantic request validation failures (bad query params,
        # malformed request bodies, etc.) previously fell through to
        # FastAPI's default {"detail": [...]} shape. Normalized to the
        # same envelope as everything else, keeping the raw per-field
        # errors available under error.details for anything that wants
        # them.
        message = "Request validation failed"
        return JSONResponse(
            status_code=422,
            content=_error_body(422, message, ErrorCode.VALIDATION_ERROR, details=jsonable_encoder(exc.errors(), custom_encoder={Exception: str})),
        )
