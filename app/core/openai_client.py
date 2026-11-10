"""One shared async OpenAI client."""

from functools import lru_cache

from openai import AsyncOpenAI, OpenAIError

from app.core.config import get_settings
from app.core.errors import AppError


@lru_cache
def get_openai_client() -> AsyncOpenAI:
    settings = get_settings()
    try:
        # If openai_api_key is None the SDK falls back to the OPENAI_API_KEY env var.
        return AsyncOpenAI(
            api_key=settings.openai_api_key or None,
            timeout=settings.openai_timeout_seconds,
            max_retries=2,
        )
    except OpenAIError as exc:
        raise AppError(
            "openai_key_missing",
            "OPENAI_API_KEY is not set. Set it in the environment or in a .env file.",
            500,
        ) from exc
