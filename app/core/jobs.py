"""The scheduled job: refresh the research feed, then (optionally) run the agents after it."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


async def refresh_feed_then_agent(
    refresh_feed: Callable[[], Awaitable[object]],
    run_agent: Callable[[], Awaitable[object]] | None,
    *more_agents: Callable[[], Awaitable[object]] | None,
) -> None:
    """If the feed refresh fails, the error is raised (the scheduler retries it) and the agents are
    skipped. If an agent fails, we log it and carry on with the next one, and the job stays
    successful, so the scheduler does not repeat an expensive feed search because of an analysis error."""
    await refresh_feed()
    for agent in (run_agent, *more_agents):
        if agent is None:
            continue
        try:
            await agent()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Agent run failed after the feed refresh: %s: %s", type(exc).__name__, exc)
