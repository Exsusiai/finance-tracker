"""Unified exception handlers and custom exceptions."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Base application error."""

    def __init__(self, code: str, message: str, status_code: int = 400, details: dict | None = None):
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: int | str):
        super().__init__(
            code="NOT_FOUND",
            message=f"{resource} with id={resource_id} not found",
            status_code=404,
        )


class ConflictError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(code="CONFLICT", message=message, status_code=409, details=details)


class ParserError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(code="PARSER_ERROR", message=message, status_code=422, details=details)


class InvalidInputError(AppError):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(code="INVALID_INPUT", message=message, status_code=422, details=details)


class MarketDataError(AppError):
    def __init__(self, message: str):
        super().__init__(code="MARKET_DATA_ERROR", message=message, status_code=502)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            },
        )

    @app.exception_handler(IntegrityError)
    async def _integrity_error_handler(request: Request, exc: IntegrityError):
        # Sprint 4 FIX-25 (review V3 §V3-P3-1): the previous handler returned
        # `details.original = str(exc.orig)`, which leaks table/column/index
        # names and raw DBAPI strings to the client. Log them server-side
        # for debugging, but only return a stable generic message + a high-
        # level violation kind to callers.
        msg = str(exc.orig) if exc.orig else str(exc)
        path = request.url.path

        if "UNIQUE constraint failed" in msg:
            logger.warning(
                "integrity_unique_violation", path=path, db_message=msg
            )
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": {
                        "code": "CONFLICT",
                        "message": "Resource already exists or violates uniqueness constraint.",
                        "details": {"kind": "unique_violation"},
                    },
                },
            )
        if "FOREIGN KEY constraint failed" in msg:
            logger.warning(
                "integrity_fk_violation", path=path, db_message=msg
            )
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": "Referenced resource does not exist.",
                        "details": {"kind": "foreign_key_violation"},
                    },
                },
            )
        logger.warning("integrity_generic", path=path, db_message=msg)
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "Database integrity error.",
                    "details": {"kind": "integrity_error"},
                },
            },
        )

    @app.exception_handler(Exception)
    async def _generic_error_handler(request: Request, exc: Exception):
        # Avoid leaking internal details in production
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "An unexpected error occurred",
                    "details": None,
                },
            },
        )
