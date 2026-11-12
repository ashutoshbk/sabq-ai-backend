"""Settings loaded from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


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
    trends_max_output_tokens: int = 8000

    # Research feed
    trends_per_topic: int = Field(default=10, ge=1, le=30)
    trends_search_concurrency: int = Field(default=3, ge=1, le=10)
    trends_store_path: str = "data/trends_store.json"  # empty = do not save to a file
    trends_cache_ttl_minutes: int = 1440

    # Automatic refresh of the research feed
    trends_auto_refresh_enabled: bool = True
    trends_refresh_frequency: str = "weekly"  # daily | weekly
    trends_refresh_day: str = "monday"  # used when weekly
    trends_refresh_time: str = "06:00"  # HH:MM, 24-hour clock
    trends_refresh_timezone: str = "Asia/Dubai"
    trends_refresh_on_startup: bool = False

    openai_timeout_seconds: float = 180.0

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

    @field_validator("trends_refresh_frequency")
    @classmethod
    def _check_frequency(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ("daily", "weekly"):
            raise ValueError("TRENDS_REFRESH_FREQUENCY must be daily or weekly")
        return value

    @field_validator("trends_refresh_day")
    @classmethod
    def _check_day(cls, value: str) -> str:
        value = value.strip().lower()
        for day in DAYS:
            if value == day or value == day[:3]:
                return day
        raise ValueError("TRENDS_REFRESH_DAY must be a weekday name, for example monday")

    @property
    def refresh_hour_minute(self) -> tuple[int, int]:
        hour, minute = self.trends_refresh_time.split(":")
        return int(hour), int(minute)

    @property
    def refresh_weekday(self) -> int | None:
        """0 = Monday ... 6 = Sunday for weekly refresh, None for daily."""
        if self.trends_refresh_frequency == "daily":
            return None
        return DAYS.index(self.trends_refresh_day)

    @property
    def effective_cache_ttl_minutes(self) -> int:
        """How long saved results count as fresh.

        With automatic refresh on, results must outlive one refresh period (plus 2 hours),
        or they would be called stale before the next refresh and a visitor would wait
        for a live search."""
        ttl = self.trends_cache_ttl_minutes
        if ttl <= 0 or not self.trends_auto_refresh_enabled:
            return ttl
        period = 1440 if self.trends_refresh_frequency == "daily" else 7 * 1440
        return max(ttl, period + 120)

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
