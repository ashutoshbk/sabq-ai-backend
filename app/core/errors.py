"""One error shape for the whole API, so the front-end can show something sensible.

Every error response looks like:
    {"error": {"code": "some_code", "message": "Human readable text"}}
"""

from __future__ import annotations

import logging

import openai
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 500):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _response(status_code: int, code: str, message: str, **extra) -> JSONResponse:
    body = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(status_code=status_code, content=body)


def openai_error_to_response(exc: openai.OpenAIError) -> JSONResponse:
    """Translate OpenAI SDK errors into clean, specific API errors."""
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return _response(
            502,
            "openai_auth_error",
            "OpenAI rejected the API key, or the key has no access to the requested "
            "model or tool. Check OPENAI_API_KEY and the model names.",
        )
    if isinstance(exc, openai.RateLimitError):
        return _response(
            429,
            "openai_rate_limited",
            "OpenAI rate limit or quota reached. Wait a moment and try again, "
            "or check the account's billing and quota.",
        )
    if isinstance(exc, openai.NotFoundError):
        return _response(
            502,
            "openai_model_not_found",
            "The configured model was not found for this key. Change CHAT_MODEL "
            "or TRENDS_MODEL in the environment.",
        )
    if isinstance(exc, openai.BadRequestError):
        return _response(502, "openai_bad_request", f"OpenAI rejected the request: {exc.message}")
    if isinstance(exc, openai.APITimeoutError):
        return _response(504, "openai_timeout", "OpenAI took too long to respond. Try again.")
    if isinstance(exc, openai.APIConnectionError):
        return _response(503, "openai_unreachable", "Could not reach OpenAI. Check the network.")
    if isinstance(exc, openai.APIStatusError):
        return _response(502, "openai_error", f"OpenAI returned an error ({exc.status_code}).")
    return _response(500, "internal_error", "Unexpected error while calling OpenAI.")


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return _response(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
            for e in exc.errors()
        ]
        return _response(422, "invalid_request", "The request body is invalid.", details=details)

    @app.exception_handler(openai.OpenAIError)
    async def _openai_error(_: Request, exc: openai.OpenAIError):
        logger.warning("OpenAI error: %s: %s", type(exc).__name__, exc)
        return openai_error_to_response(exc)

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        logger.exception("Unhandled error")
        return _response(500, "internal_error", "Something went wrong on the server.")
