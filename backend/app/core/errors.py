"""Unified exception handlers and custom exceptions."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError


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
        msg = str(exc.orig) if exc.orig else str(exc)
        # Try to detect unique constraint violations
        if "UNIQUE constraint failed" in msg:
            return JSONResponse(
                status_code=409,
                content={
                    "success": False,
                    "error": {
                        "code": "CONFLICT",
                        "message": "Unique constraint violation",
                        "details": {"original": msg},
                    },
                },
            )
        if "FOREIGN KEY constraint failed" in msg:
            return JSONResponse(
                status_code=422,
                content={
                    "success": False,
                    "error": {
                        "code": "INVALID_INPUT",
                        "message": "Referenced resource does not exist",
                        "details": {"original": msg},
                    },
                },
            )
        return JSONResponse(
            status_code=400,
            content={
                "success": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "Database integrity error",
                    "details": {"original": msg},
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
