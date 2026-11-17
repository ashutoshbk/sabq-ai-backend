"""The scheduled job: refresh the research feed, then (optionally) run the Horizon agent on it."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


async def refresh_feed_then_agent(
    refresh_feed: Callable[[], Awaitable[object]],
    run_agent: Callable[[], Awaitable[object]] | None,
) -> None:
    """If the feed refresh fails, the error is raised (the scheduler retries it) and the agent is
    skipped. If only the agent fails, we log it and keep the job successful, so the scheduler does
    not repeat an expensive feed search just because of an analysis error."""
    await refresh_feed()
    if run_agent is None:
        return
    try:
        await run_agent()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Horizon agent run failed after the feed refresh: %s: %s", type(exc).__name__, exc)
