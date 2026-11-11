import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.scheduler import DailyRefresher, next_run_after, resolve_timezone

TZ = timezone(timedelta(hours=4))


class Clock:
    """A fake clock: sleeping moves time forward and gives other tasks a turn."""

    def __init__(self, start: datetime):
        self.t = start
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += timedelta(seconds=seconds)
        await asyncio.sleep(0)


def make_refresher(refresh, clock, **kw):
    return DailyRefresher(refresh, 6, 0, TZ, "Asia/Dubai", now_fn=clock.now, sleep=clock.sleep, **kw)


# ---- time maths ---------------------------------------------------------

def test_next_run_later_today():
    now = datetime(2026, 11, 11, 5, 0, tzinfo=TZ)
    assert next_run_after(now, 6, 0) == datetime(2026, 11, 11, 6, 0, tzinfo=TZ)


def test_next_run_tomorrow_when_time_has_passed():
    now = datetime(2026, 11, 11, 7, 30, tzinfo=TZ)
    assert next_run_after(now, 6, 0) == datetime(2026, 11, 12, 6, 0, tzinfo=TZ)


def test_next_run_exactly_at_the_time_means_tomorrow():
    now = datetime(2026, 11, 11, 6, 0, tzinfo=TZ)
    assert next_run_after(now, 6, 0) == datetime(2026, 11, 12, 6, 0, tzinfo=TZ)


def test_month_rollover():
    now = datetime(2026, 11, 30, 23, 0, tzinfo=TZ)
    assert next_run_after(now, 6, 0) == datetime(2026, 12, 1, 6, 0, tzinfo=TZ)


def test_resolve_timezone():
    assert datetime(2026, 11, 11, tzinfo=resolve_timezone("Asia/Dubai")).utcoffset() == timedelta(hours=4)
    assert resolve_timezone("Not/AZone") is timezone.utc


# ---- the loop -----------------------------------------------------------

def test_waits_for_the_time_then_refreshes_once():
    clock = Clock(datetime(2026, 11, 11, 5, 58, tzinfo=TZ))
    done, calls = asyncio.Event(), []

    async def refresh():
        calls.append(clock.now())
        done.set()

    async def scenario():
        r = make_refresher(refresh, clock)
        task = asyncio.create_task(r.run())
        await asyncio.wait_for(done.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return r

    r = asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0] >= datetime(2026, 11, 11, 6, 0, tzinfo=TZ)
    assert calls[0] < datetime(2026, 11, 11, 6, 2, tzinfo=TZ)
    assert r.last_result == "ok"
    assert r.next_run == datetime(2026, 11, 12, 6, 0, tzinfo=TZ)
    assert max(clock.sleeps) <= 60


def test_refreshes_immediately_on_startup_when_asked():
    clock = Clock(datetime(2026, 11, 11, 12, 0, tzinfo=TZ))
    done, sleeps_before = asyncio.Event(), []

    async def refresh():
        sleeps_before.append(len(clock.sleeps))
        done.set()

    async def scenario():
        task = asyncio.create_task(make_refresher(refresh, clock, run_on_startup=True).run())
        await asyncio.wait_for(done.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert sleeps_before == [0]


def test_retries_then_succeeds():
    clock = Clock(datetime(2026, 11, 11, 6, 0, tzinfo=TZ))
    attempts = []

    async def refresh():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("boom")

    r = make_refresher(refresh, clock, retry_minutes=30, max_retries=3)
    assert asyncio.run(r._refresh_with_retries()) is True
    assert len(attempts) == 3
    assert clock.sleeps == [1800, 1800]
    assert r.last_result == "ok"


def test_gives_up_after_max_retries_and_keeps_going():
    clock = Clock(datetime(2026, 11, 11, 6, 0, tzinfo=TZ))
    attempts = []

    async def refresh():
        attempts.append(1)
        raise RuntimeError("always broken")

    r = make_refresher(refresh, clock, max_retries=2)
    assert asyncio.run(r._refresh_with_retries()) is False
    assert len(attempts) == 3
    assert r.last_result.startswith("failed: RuntimeError")


def test_status_shape():
    clock = Clock(datetime(2026, 11, 11, 6, 0, tzinfo=TZ))
    status = make_refresher(lambda: None, clock).status()
    assert status["enabled"] is True
    assert status["time"] == "06:00"
    assert status["timezone"] == "Asia/Dubai"
    assert status["next_run"] is None and status["last_run"] is None


# ---- settings -----------------------------------------------------------

def test_refresh_time_is_validated_and_normalised():
    assert Settings(_env_file=None, trends_refresh_time="6:5").trends_refresh_time == "06:05"
    for bad in ("25:00", "06:60", "6", "abc", ""):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, trends_refresh_time=bad)


def test_cache_outlives_a_day_when_daily_refresh_is_on():
    on = Settings(_env_file=None, trends_daily_refresh_enabled=True, trends_cache_ttl_minutes=360)
    off = Settings(_env_file=None, trends_daily_refresh_enabled=False, trends_cache_ttl_minutes=360)
    disabled = Settings(_env_file=None, trends_daily_refresh_enabled=True, trends_cache_ttl_minutes=0)
    assert on.effective_cache_ttl_minutes == 26 * 60
    assert off.effective_cache_ttl_minutes == 360
    assert disabled.effective_cache_ttl_minutes == 0


# ---- wired into the app -------------------------------------------------

def test_app_starts_and_stops_the_scheduler(monkeypatch):
    from app.main import create_app

    monkeypatch.setenv("TRENDS_DAILY_REFRESH_ENABLED", "true")
    monkeypatch.setenv("TRENDS_REFRESH_TIME", "07:30")
    monkeypatch.setenv("TRENDS_REFRESH_ON_STARTUP", "false")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            info = client.get("/health").json()["trends_refresh"]
        assert info["enabled"] is True
        assert info["time"] == "07:30"
        assert info["timezone"] == "Asia/Dubai"
    finally:
        get_settings.cache_clear()


def test_health_says_disabled_when_off(api):
    client, _ = api
    assert client.get("/health").json()["trends_refresh"] == {"enabled": False}
