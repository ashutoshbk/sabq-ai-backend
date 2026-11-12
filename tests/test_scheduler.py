import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.core.scheduler import ScheduledRefresher, next_run_after, resolve_timezone

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
    return ScheduledRefresher(refresh, 6, 0, TZ, "Asia/Dubai", now_fn=clock.now, sleep=clock.sleep, **kw)


# ---- time maths: daily --------------------------------------------------

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


# ---- time maths: weekly (2026-11-11 is a Wednesday, Monday = 0) ---------

def test_weekly_goes_to_the_next_monday():
    now = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)
    assert next_run_after(now, 6, 0, weekday=0) == datetime(2026, 11, 16, 6, 0, tzinfo=TZ)


def test_weekly_same_day_if_the_time_has_not_come():
    now = datetime(2026, 11, 16, 5, 0, tzinfo=TZ)  # a Monday
    assert next_run_after(now, 6, 0, weekday=0) == datetime(2026, 11, 16, 6, 0, tzinfo=TZ)


def test_weekly_next_week_if_the_time_has_passed():
    now = datetime(2026, 11, 16, 7, 0, tzinfo=TZ)  # a Monday
    assert next_run_after(now, 6, 0, weekday=0) == datetime(2026, 11, 23, 6, 0, tzinfo=TZ)


def test_weekly_on_the_same_weekday_as_today_wednesday():
    now = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)  # Wednesday after the time
    assert next_run_after(now, 6, 0, weekday=2) == datetime(2026, 11, 18, 6, 0, tzinfo=TZ)


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


def test_weekly_refresher_waits_for_its_day():
    clock = Clock(datetime(2026, 11, 15, 23, 0, tzinfo=TZ))  # Sunday night
    done, calls = asyncio.Event(), []

    async def refresh():
        calls.append(clock.now())
        done.set()

    async def scenario():
        r = make_refresher(refresh, clock, weekday=0)  # Monday 06:00
        task = asyncio.create_task(r.run())
        await asyncio.wait_for(done.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return r

    r = asyncio.run(scenario())
    assert calls[0].date() == datetime(2026, 11, 16).date()
    assert r.next_run == datetime(2026, 11, 23, 6, 0, tzinfo=TZ)


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
    daily = make_refresher(lambda: None, clock).status()
    assert daily["enabled"] is True and daily["frequency"] == "daily" and daily["day"] is None
    assert daily["time"] == "06:00" and daily["timezone"] == "Asia/Dubai"
    assert daily["next_run"] is None and daily["last_run"] is None

    weekly = make_refresher(lambda: None, clock, weekday=0).status()
    assert weekly["frequency"] == "weekly" and weekly["day"] == "monday"


# ---- settings -----------------------------------------------------------

def test_refresh_time_is_validated_and_normalised():
    assert Settings(_env_file=None, trends_refresh_time="6:5").trends_refresh_time == "06:05"
    for bad in ("25:00", "06:60", "6", "abc", ""):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, trends_refresh_time=bad)


def test_frequency_and_day_are_validated():
    s = Settings(_env_file=None, trends_refresh_frequency="Weekly", trends_refresh_day="Fri")
    assert s.trends_refresh_frequency == "weekly" and s.trends_refresh_day == "friday"
    assert s.refresh_weekday == 4
    assert Settings(_env_file=None, trends_refresh_frequency="daily").refresh_weekday is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trends_refresh_frequency="monthly")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, trends_refresh_day="someday")


def test_results_stay_fresh_for_a_whole_refresh_period():
    base = dict(_env_file=None, trends_cache_ttl_minutes=360)
    weekly = Settings(**base, trends_auto_refresh_enabled=True, trends_refresh_frequency="weekly")
    daily = Settings(**base, trends_auto_refresh_enabled=True, trends_refresh_frequency="daily")
    off = Settings(**base, trends_auto_refresh_enabled=False)
    disabled = Settings(_env_file=None, trends_auto_refresh_enabled=True, trends_cache_ttl_minutes=0)
    assert weekly.effective_cache_ttl_minutes == 7 * 1440 + 120
    assert daily.effective_cache_ttl_minutes == 1440 + 120
    assert off.effective_cache_ttl_minutes == 360
    assert disabled.effective_cache_ttl_minutes == 0


# ---- wired into the app -------------------------------------------------

def test_app_starts_and_stops_the_scheduler(monkeypatch):
    from app.main import create_app

    monkeypatch.setenv("TRENDS_AUTO_REFRESH_ENABLED", "true")
    monkeypatch.setenv("TRENDS_REFRESH_FREQUENCY", "weekly")
    monkeypatch.setenv("TRENDS_REFRESH_DAY", "friday")
    monkeypatch.setenv("TRENDS_REFRESH_TIME", "07:30")
    monkeypatch.setenv("TRENDS_REFRESH_ON_STARTUP", "false")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            info = client.get("/health").json()["trends_refresh"]
        assert info["enabled"] is True
        assert info["frequency"] == "weekly" and info["day"] == "friday"
        assert info["time"] == "07:30" and info["timezone"] == "Asia/Dubai"
    finally:
        get_settings.cache_clear()


def test_health_says_disabled_when_off(api):
    client, _ = api
    assert client.get("/health").json()["trends_refresh"] == {"enabled": False}
