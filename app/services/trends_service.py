"""Research feed: up to N research items for each of the five topics.

The model does the searching and writes titles and summaries. It does NOT get to decide sources:
  - an item is kept only if its URL was actually cited/read by the search tool
  - and only if that URL is on an allowed domain
  - the source name comes from our own domain list, not from the model
  - a date is kept only if it is well-formed and not in the future

Each refresh ADDS items it has not seen before and keeps the rest (newest first, capped at N),
so a weekly refresh that finds nothing new leaves the results unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import DomainEntry, Topic, load_prompt
from app.core.store import JsonFileStore
from app.schemas.trends import StoredItem, TopicState, TopicTrends, TrendItem, TrendsResponse
from app.services.trends_parsing import (
    CATEGORIES,
    MAX_SUMMARY_CHARS,
    MAX_TITLE_CHARS,
    clean_url,
    coerce_choice,
    collect_citation_urls,
    match_domain,
    normalize_date,
    parse_cards_json,
    shorten,
    url_key,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # per topic: one search, plus one more if it found fewer than wanted
RETRY_AFTER_FAILURE = timedelta(minutes=10)  # do not hammer OpenAI after a failed refresh
STORE_VERSION = 1


def date_sort_key(value: str | None) -> str:
    """Sortable form of YYYY / YYYY-MM / YYYY-MM-DD. Missing dates sort last."""
    if not value:
        return ""
    return "-".join((value.split("-") + ["01", "01"])[:3])


class TrendsService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        domains: list[DomainEntry],
        topics: list[Topic],
        store: JsonFileStore | None = None,
        prompt_template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._domains = domains
        self._topics = topics
        self._store = store
        self._template = Template(prompt_template or load_prompt("trends_search.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._state: dict[str, TopicState] = {t.slug: TopicState() for t in topics}
        self._failed_at: dict[str, datetime] = {}
        self._lock = asyncio.Lock()
        self._load()

    @property
    def topics(self) -> list[Topic]:
        return list(self._topics)

    # -- public -----------------------------------------------------------

    async def get_trends(self, topic: str | None = None, refresh: bool = False) -> TrendsResponse:
        selected = self._select(topic)
        searched = False

        if refresh or any(self._is_stale(t.slug) for t in selected):
            async with self._lock:
                # Another request may have refreshed while we waited for the lock.
                targets = selected if refresh else [t for t in selected if self._is_stale(t.slug)]
                if targets:
                    try:
                        await self._refresh(targets)
                        searched = True
                    except Exception:
                        # A background top-up failed: serve what we have if there is anything.
                        if refresh or not any(self._state[t.slug].items for t in selected):
                            raise
                        logger.warning("Refresh failed; serving earlier results", exc_info=True)

        return self._response(selected, searched)

    # -- picking and freshness --------------------------------------------

    def _select(self, slug: str | None) -> list[Topic]:
        if slug is None:
            return list(self._topics)
        wanted = slug.strip().lower()
        for topic in self._topics:
            if topic.slug == wanted:
                return [topic]
        raise AppError(
            "unknown_topic",
            f"Unknown topic '{slug}'. Valid topics: " + ", ".join(t.slug for t in self._topics),
            404,
        )

    def _is_stale(self, slug: str) -> bool:
        state = self._state[slug]
        now = self._now()
        failed_at = self._failed_at.get(slug)
        if state.items and failed_at and now - failed_at < RETRY_AFTER_FAILURE:
            return False
        if not state.last_checked:
            return True
        ttl = self._settings.effective_cache_ttl_minutes
        if ttl <= 0:
            return True
        return now - datetime.fromisoformat(state.last_checked) >= timedelta(minutes=ttl)

    # -- refreshing --------------------------------------------------------

    async def _refresh(self, targets: list[Topic]) -> None:
        semaphore = asyncio.Semaphore(self._settings.trends_search_concurrency)

        async def one(topic: Topic) -> list[dict]:
            async with semaphore:
                return await self._research(topic)

        results = await asyncio.gather(*(one(t) for t in targets), return_exceptions=True)
        now = self._now()
        stamp = now.isoformat(timespec="seconds")
        failures: list[Exception] = []

        for topic, result in zip(targets, results):
            state = self._state[topic.slug]
            if isinstance(result, Exception):
                logger.warning("Research for %s failed: %s", topic.slug, result)
                state.last_error = f"{type(result).__name__}: {result}"[:200]
                self._failed_at[topic.slug] = now
                failures.append(result)
                continue
            if isinstance(result, BaseException):  # for example cancellation: pass it on
                raise result
            state.last_error = None
            self._failed_at.pop(topic.slug, None)
            state.last_checked = stamp
            added = self._merge(state, result, stamp)
            state.last_new_count = added
            if added:
                state.last_changed = stamp
            logger.info("%s: %d new, %d kept", topic.slug, added, len(state.items))

        self._save()

        if failures and len(failures) == len(targets):
            raise failures[0]
        if not any(self._state[t.slug].items for t in targets):
            raise AppError(
                "no_valid_trends",
                "The search did not return any research backed by an allowed source. Try again.",
                502,
            )

    async def _research(self, topic: Topic) -> list[dict]:
        """Search one topic (up to two passes) and return validated, new candidate items."""
        wanted = self._settings.trends_per_topic
        known = {url_key(i.source_url): i.source_url for i in self._state[topic.slug].items}
        found: dict[str, dict] = {}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            avoid = list(known.values()) + [c["source_url"] for c in found.values()]
            text, dump = await self._search(topic, avoid)
            for item in self._validated_items(text, dump):
                key = url_key(item["source_url"])
                if key in known or key in found:
                    continue
                found[key] = item
            logger.info("%s attempt %d: %d new candidates (%d already known)",
                        topic.slug, attempt, len(found), len(known))
            if len(found) + len(known) >= wanted:
                break
        return list(found.values())

    async def _search(self, topic: Topic, avoid: list[str]) -> tuple[str, dict]:
        extra = ""
        if avoid:
            extra = "- Do not return these pages again (already collected); find different ones: " + ", ".join(avoid[:30])
        prompt = self._template.safe_substitute(
            today=self._now().strftime("%d %B %Y"),
            topic=topic.name,
            focus=topic.focus,
            count=self._settings.trends_per_topic,
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

    def _validated_items(self, text: str, dump: dict) -> list[dict]:
        """Turn the model's JSON into items, dropping anything we cannot verify."""
        cited = collect_citation_urls(dump)
        today = self._now().date()
        items: list[dict] = []

        for raw in parse_cards_json(text):
            title = shorten(raw.get("title"), MAX_TITLE_CHARS)
            summary = shorten(raw.get("summary"), MAX_SUMMARY_CHARS)
            url = clean_url(raw.get("source_url"))

            if not title or not summary or not url:
                logger.info("Dropped item: missing title, summary or url")
                continue
            if url_key(url) not in cited:
                logger.info("Dropped item: URL was not cited by the search tool: %s", url)
                continue
            domain = match_domain(url, self._domains)
            if domain is None:
                logger.info("Dropped item: domain not allowed: %s", url)
                continue

            items.append(
                {
                    "title": title,
                    "summary": summary,
                    "category": coerce_choice(raw.get("category"), CATEGORIES, "General"),
                    "published_date": normalize_date(raw.get("published_date"), today),
                    "source_name": domain.name,
                    "source_url": url,
                }
            )
        return items

    def _merge(self, state: TopicState, candidates: list[dict], stamp: str) -> int:
        """Add items we have not seen, keep the newest N. Returns how many new ones were kept."""
        known = {url_key(i.source_url) for i in state.items}
        fresh = [
            StoredItem(**c, first_seen=stamp)
            for c in candidates
            if url_key(c["source_url"]) not in known
        ]
        merged = state.items + fresh
        merged.sort(key=lambda i: (date_sort_key(i.published_date), i.first_seen), reverse=True)
        kept = merged[: self._settings.trends_per_topic]
        fresh_ids = {id(i) for i in fresh}
        state.items = kept
        return sum(1 for i in kept if id(i) in fresh_ids)

    # -- answering ---------------------------------------------------------

    def _response(self, selected: list[Topic], searched: bool) -> TrendsResponse:
        topics: list[TopicTrends] = []
        for topic in selected:
            state = self._state[topic.slug]
            items = [TrendItem(id=n, **i.model_dump()) for n, i in enumerate(state.items, start=1)]
            warning = None
            if state.last_error:
                warning = (
                    "The last refresh failed; showing earlier results."
                    if items
                    else "The last refresh failed and there are no earlier results."
                )
            elif not items:
                warning = "No verified research sources were found for this topic yet."
            topics.append(
                TopicTrends(
                    topic=topic.slug,
                    name=topic.name,
                    count=len(items),
                    items=items,
                    last_checked=state.last_checked,
                    last_changed=state.last_changed,
                    new_in_last_refresh=state.last_new_count,
                    warning=warning,
                )
            )
        checked = [t.last_checked for t in topics if t.last_checked]
        problems = [t.name for t in topics if t.warning]
        return TrendsResponse(
            topics=topics,
            total=sum(t.count for t in topics),
            generated_at=max(checked) if checked else self._now().isoformat(timespec="seconds"),
            cached=not searched,
            model=self._settings.trends_model,
            warning=("Some topics have a warning: " + ", ".join(problems)) if problems else None,
        )

    # -- saving ------------------------------------------------------------

    def _load(self) -> None:
        if not self._store:
            return
        for slug, data in (self._store.load().get("topics") or {}).items():
            if slug not in self._state:
                continue
            try:
                self._state[slug] = TopicState.model_validate(data)
            except Exception:  # noqa: BLE001 - a damaged entry must not stop the server
                logger.warning("Ignoring damaged saved data for topic %s", slug)

    def _save(self) -> None:
        if not self._store:
            return
        self._store.save(
            {
                "version": STORE_VERSION,
                "saved_at": self._now().isoformat(timespec="seconds"),
                "topics": {
                    slug: state.model_dump(exclude={"last_error"})
                    for slug, state in self._state.items()
                },
            }
        )
