"""Settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Read from the OPENAI_API_KEY environment variable. Never logged or returned.
    openai_api_key: str | None = None

    chat_model: str = "gpt-5.4-nano"
    trends_model: str = "gpt-5.4-mini"
    reasoning_effort: str | None = None

    chat_max_output_tokens: int = 1500
    trends_max_output_tokens: int = 4000

    trends_count: int = 3
    trends_cache_ttl_minutes: int = 360

    openai_timeout_seconds: float = 90.0

    allowed_origins: str = "*"
    log_level: str = "INFO"

    @property
    def cors_origins(self) -> list[str]:
        origins = [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
        return origins or ["*"]

    @property
    def reasoning(self) -> dict | None:
        """Extra `reasoning` argument for the Responses API, or None."""
        effort = (self.reasoning_effort or "").strip()
        return {"effort": effort} if effort else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
