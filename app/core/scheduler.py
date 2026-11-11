"""Runs one job every day at a fixed wall-clock time (used to refresh the news feed).

It lives inside the server process, so the server must be running at that time.
If the refresh fails it keeps the previous cached result and retries a few times.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

CHECK_EVERY_SECONDS = 60  # wake up this often to compare against the real clock


def resolve_timezone(name: str) -> tzinfo:
    """Look up an IANA time zone. Windows has no zone database unless `tzdata` is installed."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if name == "Asia/Dubai":  # UAE has no daylight saving: always UTC+4
            logger.warning("Time zone database not found; using fixed UTC+4 for Asia/Dubai. "
                           "Fix with: pip install tzdata")
            return timezone(timedelta(hours=4), "GST")
        logger.warning("Time zone %r not found; falling back to UTC. "
                       "On Windows run: pip install tzdata", name)
        return timezone.utc


def next_run_after(now: datetime, hour: int, minute: int) -> datetime:
    """The next occurrence of hour:minute strictly after `now` (same time zone as `now`)."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class DailyRefresher:
    def __init__(
        self,
        refresh: Callable[[], Awaitable[object]],
        hour: int,
        minute: int,
        tz: tzinfo,
        tz_name: str,
        run_on_startup: bool = False,
        retry_minutes: int = 30,
        max_retries: int = 3,
        now_fn: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._refresh = refresh
        self.hour, self.minute = hour, minute
        self.tz_name = tz_name
        self._run_on_startup = run_on_startup
        self._retry_minutes = retry_minutes
        self._max_retries = max_retries
        self._now = now_fn or (lambda: datetime.now(tz))
        self._sleep = sleep

        self.next_run: datetime | None = None
        self.last_run: datetime | None = None
        self.last_result: str | None = None

    def status(self) -> dict:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat(timespec="seconds") if value else None

        return {
            "enabled": True,
            "time": f"{self.hour:02d}:{self.minute:02d}",
            "timezone": self.tz_name,
            "next_run": iso(self.next_run),
            "last_run": iso(self.last_run),
            "last_result": self.last_result,
        }

    async def run(self) -> None:
        if self._run_on_startup:
            await self._refresh_with_retries()
        while True:
            target = next_run_after(self._now(), self.hour, self.minute)
            self.next_run = target
            logger.info("Next news refresh at %s", target.isoformat(timespec="minutes"))
            await self._wait_until(target)
            await self._refresh_with_retries()

    async def _wait_until(self, target: datetime) -> None:
        # Short sleeps against the wall clock, so a laptop that slept still fires on time.
        while True:
            remaining = (target - self._now()).total_seconds()
            if remaining <= 0:
                return
            await self._sleep(min(remaining, CHECK_EVERY_SECONDS))

    async def _refresh_with_retries(self) -> bool:
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                self.last_run = self._now()
                self.last_result = f"failed: {type(exc).__name__}: {exc}"[:200]
                logger.warning("News refresh failed (attempt %d/%d): %s", attempt, attempts, exc)
                if attempt < attempts:
                    await self._sleep(self._retry_minutes * 60)
            else:
                self.last_run = self._now()
                self.last_result = "ok"
                logger.info("News refresh done")
                return True
        logger.error("News refresh gave up; the previous cached result stays in use")
        return False
