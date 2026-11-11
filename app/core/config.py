"""Settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic import field_validator
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

    # Built-in daily refresh of the news feed
    trends_daily_refresh_enabled: bool = True
    trends_refresh_time: str = "06:00"  # HH:MM, 24-hour clock
    trends_refresh_timezone: str = "Asia/Dubai"
    trends_refresh_on_startup: bool = False

    openai_timeout_seconds: float = 90.0

    allowed_origins: str = "*"
    log_level: str = "INFO"

    @field_validator("trends_refresh_time")
    @classmethod
    def _check_refresh_time(cls, value: str) -> str:
        message = "TRENDS_REFRESH_TIME must look like HH:MM, for example 06:00"
        try:
            hour_text, minute_text = value.strip().split(":")
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            raise ValueError(message) from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(message)
        return f"{hour:02d}:{minute:02d}"

    @property
    def refresh_hour_minute(self) -> tuple[int, int]:
        hour, minute = self.trends_refresh_time.split(":")
        return int(hour), int(minute)

    @property
    def effective_cache_ttl_minutes(self) -> int:
        """With a daily refresh on, the cache must outlive 24 hours, or it would expire
        before the next refresh and make a visitor wait for a live search."""
        ttl = self.trends_cache_ttl_minutes
        if ttl <= 0 or not self.trends_daily_refresh_enabled:
            return ttl
        return max(ttl, 26 * 60)

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
