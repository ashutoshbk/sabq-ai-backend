"""News feed: asks OpenAI web search for trends, then VALIDATES everything in code.

The model writes titles and summaries. It does NOT get to decide sources:
  - a card is kept only if its URL was actually cited/read by the search tool
  - and only if that URL is on an allowed domain
  - the source name comes from our own domain list, not from the model
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.cache import SingleValueTTLCache
from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import DomainEntry, load_prompt
from app.schemas.trends import TrendCard, TrendsResponse
from app.services.trends_parsing import (
    CATEGORIES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    TIMEFRAMES,
    clean_url,
    coerce_choice,
    collect_citation_urls,
    match_domain,
    parse_cards_json,
    shorten,
    url_key,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # one try plus one retry if fewer than `trends_count` valid cards


class TrendsService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        domains: list[DomainEntry],
        prompt_template: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._settings = settings
        self._domains = domains
        self._template = Template(prompt_template or load_prompt("trends_search.md"))
        self._cache: SingleValueTTLCache[TrendsResponse] = SingleValueTTLCache(
            ttl_seconds=settings.effective_cache_ttl_minutes * 60, clock=clock
        )
        self._lock = asyncio.Lock()

    # -- public -----------------------------------------------------------

    async def get_trends(self, refresh: bool = False) -> TrendsResponse:
        if not refresh:
            cached = self._cache.get()
            if cached is not None:
                return cached.model_copy(update={"cached": True})

        async with self._lock:
            # Another request may have filled the cache while we waited.
            if not refresh:
                cached = self._cache.get()
                if cached is not None:
                    return cached.model_copy(update={"cached": True})
            result = await self._build()
            self._cache.set(result)
            return result

    # -- building the result ----------------------------------------------

    async def _build(self) -> TrendsResponse:
        wanted = self._settings.trends_count
        cards: list[TrendCard] = []
        seen_urls: set[str] = set()
        seen_titles: set[str] = set()

        for attempt in range(1, MAX_ATTEMPTS + 1):
            text, dump = await self._search(already=[c.source_url for c in cards])
            for card in self._validated_cards(text, dump):
                key = url_key(card.source_url)
                title_key = card.title.lower()
                if key in seen_urls or title_key in seen_titles:
                    continue
                seen_urls.add(key)
                seen_titles.add(title_key)
                cards.append(card)
                if len(cards) >= wanted:
                    break
            logger.info("Trends attempt %d: %d/%d valid cards", attempt, len(cards), wanted)
            if len(cards) >= wanted:
                break

        if not cards:
            raise AppError(
                "no_valid_trends",
                "The search did not return any trend backed by an allowed source. Try again.",
                502,
            )

        cards = [c.model_copy(update={"id": i}) for i, c in enumerate(cards, start=1)]
        warning = None
        if len(cards) < wanted:
            warning = f"Only {len(cards)} of {wanted} trends could be verified against allowed sources."
        return TrendsResponse(
            trends=cards,
            count=len(cards),
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            cached=False,
            model=self._settings.trends_model,
            warning=warning,
        )

    async def _search(self, already: list[str]) -> tuple[str, dict]:
        extra = ""
        if already:
            extra = "- Do not use these pages again; find different ones: " + ", ".join(already)
        prompt = self._template.safe_substitute(
            today=datetime.now(timezone.utc).strftime("%d %B %Y"),
            count=self._settings.trends_count,
            extra=extra,
        )
        kwargs = {
            "model": self._settings.trends_model,
            "input": prompt,
            "tools": [
                {
                    "type": "web_search",
                    "filters": {"allowed_domains": [d.domain for d in self._domains]},
                }
            ],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": self._settings.trends_max_output_tokens,
            "store": False,
        }
        if self._settings.reasoning:
            kwargs["reasoning"] = self._settings.reasoning

        resp = await self._client.responses.create(**kwargs)
        return (resp.output_text or ""), resp.model_dump()

    def _validated_cards(self, text: str, dump: dict) -> list[TrendCard]:
        """Turn the model's JSON into cards, dropping anything we cannot verify."""
        cited = collect_citation_urls(dump)
        cards: list[TrendCard] = []

        for item in parse_cards_json(text):
            title = shorten(item.get("title"), MAX_TITLE_CHARS)
            summary = shorten(item.get("summary"), MAX_SUMMARY_CHARS)
            url = clean_url(item.get("source_url"))

            if not title or not summary or not url:
                logger.info("Dropped card: missing title, summary or url")
                continue
            if url_key(url) not in cited:
                logger.info("Dropped card: URL was not cited by the search tool: %s", url)
                continue
            domain = match_domain(url, self._domains)
            if domain is None:
                logger.info("Dropped card: domain not allowed: %s", url)
                continue

            cards.append(
                TrendCard(
                    id=0,
                    title=title,
                    summary=summary,
                    category=coerce_choice(item.get("category"), CATEGORIES, "General"),
                    timeframe=coerce_choice(item.get("timeframe"), TIMEFRAMES, "Not specified"),
                    source_name=domain.name,
                    source_url=url,
                )
            )
        return cards
