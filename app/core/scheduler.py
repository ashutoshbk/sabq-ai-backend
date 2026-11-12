"""Runs one job on a schedule: every day, or once a week, at a fixed wall-clock time.

It lives inside the server process, so the server must be running at that time.
If the job fails it retries a few times; the previous results stay in use meanwhile.
"""

from __future__ import annotations

import asyncio
import calendar
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


def next_run_after(now: datetime, hour: int, minute: int, weekday: int | None = None) -> datetime:
    """The next occurrence of hour:minute strictly after `now`.

    weekday None = every day. 0 = Monday ... 6 = Sunday = once a week on that day.
    """
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    candidate += timedelta(days=(weekday - candidate.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


class ScheduledRefresher:
    def __init__(
        self,
        refresh: Callable[[], Awaitable[object]],
        hour: int,
        minute: int,
        tz: tzinfo,
        tz_name: str,
        weekday: int | None = None,
        run_on_startup: bool = False,
        retry_minutes: int = 30,
        max_retries: int = 3,
        now_fn: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._refresh = refresh
        self.hour, self.minute = hour, minute
        self.weekday = weekday
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
            "frequency": "daily" if self.weekday is None else "weekly",
            "day": None if self.weekday is None else calendar.day_name[self.weekday].lower(),
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
            target = next_run_after(self._now(), self.hour, self.minute, self.weekday)
            self.next_run = target
            logger.info("Next research refresh at %s", target.isoformat(timespec="minutes"))
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
                logger.warning("Research refresh failed (attempt %d/%d): %s", attempt, attempts, exc)
                if attempt < attempts:
                    await self._sleep(self._retry_minutes * 60)
            else:
                self.last_run = self._now()
                self.last_result = "ok"
                logger.info("Research refresh done")
                return True
        logger.error("Research refresh gave up; the previous results stay in use")
        return False
