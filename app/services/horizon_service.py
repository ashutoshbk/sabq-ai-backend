"""Horizon Scanning Agent.

It does NOT search the web. It reads the research items the feed (TrendsService) already stored,
and asks the model to group them into trend cards with scores and a "so what for Dubai Police" note.

Honesty rules:
  - the model only ever sees numbered items; it points at them by number
  - sources on a card (title, link, date) are copied from the stored items by number, never written
    by the model, and a card with no real source number is dropped
  - a card with a missing score is dropped, never filled in with a guess

On demand it works as a short conversation: if the request is vague it asks (up to a limit),
and then it runs. The weekly run (after the feed refresh) needs no conversation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import RESOURCES_DIR, Topic, load_prompt
from app.core.store import JsonFileStore
from app.schemas.horizon import (
    HorizonCard,
    HorizonChatRequest,
    HorizonChatResponse,
    HorizonPlan,
    HorizonResponse,
    HorizonSource,
    HorizonTopicResult,
    HorizonTopicState,
    Scores,
)
from app.services.horizon_parsing import (
    clean_plan,
    latest_possible_date,
    parse_analysis,
    parse_plan,
    rank_cards,
)
from app.services.trends_parsing import url_key

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # per topic: a second try if the first answer had no usable cards
MAX_SOURCES = 15  # research items shown to the model per topic
STORE_VERSION = 1
RANK_TEXT = {
    "relevance": "relevance to Dubai Police",
    "impact": "impact",
    "time_to_adopt": "how soon they can be adopted",
}


def load_context() -> dict[str, dict]:
    """The demo platform profile per topic (from the Sabq HTML). Missing file = no profile."""
    try:
        return json.loads((RESOURCES_DIR / "horizon_context.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def describe_context(profile: dict | None) -> str:
    if not profile:
        return "No platform profile is available for this topic."
    return (
        f"Linked process: {profile.get('linked_process', 'n/a')}. "
        f"Strategic gap: {profile.get('strategic_gap_percent', 'n/a')}%. "
        f"Adoption probability: {profile.get('adoption_probability_percent', 'n/a')}%. "
        f"Expected horizon: {profile.get('expected_horizon', 'n/a')}. "
        f"Note: {profile.get('platform_note', '')}"
    )


class HorizonService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        trends,  # anything with stored_items(slug): normally TrendsService
        topics: list[Topic],
        store: JsonFileStore | None = None,
        context: dict[str, dict] | None = None,
        analysis_template: str | None = None,
        planner_template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._trends = trends
        self._topics = topics
        self._store = store
        self._context = load_context() if context is None else context
        self._analysis = Template(analysis_template or load_prompt("horizon_analysis.md"))
        self._planner = Template(planner_template or load_prompt("horizon_planner.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._state: dict[str, HorizonTopicState] = {t.slug: HorizonTopicState() for t in topics}
        self._lock = asyncio.Lock()
        self._load()

    # -- reading (no model call) ------------------------------------------

    def get_cards(
        self, topic: str | None = None, top_n: int | None = None, rank_by: str = "relevance"
    ) -> HorizonResponse:
        return self._response(self._select([topic] if topic else None), top_n, rank_by)

    # -- running the analysis ---------------------------------------------

    async def run(
        self,
        topics: list[str] | None = None,
        force: bool = False,
        top_n: int | None = None,
        rank_by: str = "relevance",
    ) -> HorizonResponse:
        """Analyse the topics whose research changed (or all of them with force). Then answer."""
        selected = self._select(topics)
        async with self._lock:
            await self._run_locked(selected, force)
        return self._response(selected, top_n, rank_by)

    async def _run_locked(self, selected: list[Topic], force: bool) -> None:
        work: list[tuple[Topic, list, str]] = []
        for topic in selected:
            items = self._eligible_items(topic.slug)
            state = self._state[topic.slug]
            state.last_error = None
            if not items:
                continue
            signature = self._signature(items)
            if force or not state.cards or state.signature != signature:
                work.append((topic, items, signature))
        if not work:
            return

        semaphore = asyncio.Semaphore(self._settings.trends_search_concurrency)

        async def one(topic: Topic, items: list) -> list[HorizonCard]:
            async with semaphore:
                return await self._analyze(topic, items)

        results = await asyncio.gather(*(one(t, i) for t, i, _ in work), return_exceptions=True)
        stamp = self._now().isoformat(timespec="seconds")
        failures: list[Exception] = []

        for (topic, items, signature), result in zip(work, results):
            state = self._state[topic.slug]
            if isinstance(result, Exception):
                logger.warning("Horizon analysis for %s failed: %s", topic.slug, result)
                state.last_error = f"{type(result).__name__}: {result}"[:200]
                failures.append(result)
                continue
            if isinstance(result, BaseException):  # for example cancellation: pass it on
                raise result
            state.cards = result
            state.last_run = stamp
            state.signature = signature
            state.items_analyzed = len(items)
            logger.info("%s: %d trend cards from %d items", topic.slug, len(result), len(items))

        self._save()
        if failures and len(failures) == len(work):
            raise failures[0]

    def _eligible_items(self, slug: str) -> list:
        """Stored research items, newest first, without dated items older than the age limit."""
        today = self._now().date()
        cutoff = (today - timedelta(days=int(self._settings.horizon_max_age_months * 30.4375))).isoformat()
        items = []
        for item in self._trends.stored_items(slug):
            latest = latest_possible_date(item.published_date)
            if latest and latest < cutoff:
                continue  # surely too old (a bare year counts as its last day); undated items stay
            items.append(item)
        return items[:MAX_SOURCES]

    def _signature(self, items: list) -> str:
        parts = sorted(url_key(i.source_url) or i.source_url for i in items)
        parts += [str(self._settings.horizon_cards_per_topic), self._settings.horizon_model]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]

    async def _analyze(self, topic: Topic, items: list) -> list[HorizonCard]:
        today = self._now().date()
        max_cards = self._settings.horizon_cards_per_topic
        prompt = self._analysis.safe_substitute(
            today=today.strftime("%d %B %Y"),
            topic=topic.name,
            focus=topic.focus,
            max_cards=max_cards,
            context=describe_context(self._context.get(topic.slug)),
            sources=self._format_sources(items),
        )
        kwargs = {
            "model": self._settings.horizon_model,
            "input": prompt,
            "max_output_tokens": self._settings.horizon_max_output_tokens,
            "store": False,
        }
        if self._settings.reasoning:
            kwargs["reasoning"] = self._settings.reasoning

        raw_cards: list[dict] = []
        for _ in range(MAX_ATTEMPTS):
            resp = await self._client.responses.create(**kwargs)
            raw_cards = parse_analysis(resp.output_text, len(items), max_cards)
            if raw_cards:
                break
        if not raw_cards:
            raise AppError(
                "no_valid_cards",
                f"The agent did not return any usable trend cards for {topic.name}. Try again.",
                502,
            )

        stamp = self._now().isoformat(timespec="seconds")
        cards = []
        for n, raw in enumerate(raw_cards, start=1):
            sources = [
                HorizonSource(
                    ref=ref,
                    title=items[ref - 1].title,
                    source_name=items[ref - 1].source_name,
                    source_url=items[ref - 1].source_url,
                    published_date=items[ref - 1].published_date,
                )
                for ref in raw["source_refs"]
            ]
            cards.append(
                HorizonCard(
                    id=f"{topic.slug}-{n}",
                    topic=topic.slug,
                    topic_name=topic.name,
                    trend=raw["trend"],
                    theme=raw["theme"],
                    summary=raw["summary"],
                    scores=Scores(**raw["scores"]),
                    so_what_for_dubai_police=raw["so_what_for_dubai_police"],
                    sources=sources,
                    confidence=raw["confidence"],
                    generated_at=stamp,
                )
            )
        return cards

    @staticmethod
    def _format_sources(items: list) -> str:
        lines = []
        for n, item in enumerate(items, start=1):
            date_text = item.published_date or "date unknown"
            lines.append(f"[{n}] {item.title} | {item.source_name} | {date_text}\n    {item.summary}")
        return "\n".join(lines)

    # -- the on-demand conversation ----------------------------------------

    async def chat(self, req: HorizonChatRequest) -> HorizonChatResponse:
        asked = sum(1 for m in req.history if m.role == "assistant")
        must_run = asked >= self._settings.horizon_max_questions

        decision = await self._plan(req, must_run)
        model = self._settings.chat_model

        if decision is None:  # the planner's answer was unreadable
            if must_run:
                decision = {"action": "run", "message": "", "suggestions": [], "plan": None}
            else:
                return HorizonChatResponse(
                    type="question",
                    message="Sorry, I did not catch that. Which topic would you like to look at?",
                    suggestions=self._topic_suggestions(),
                    model=model,
                )

        action = decision["action"]
        if action == "run" or (must_run and action == "ask"):
            return await self._execute(decision)
        if action == "ask":
            return HorizonChatResponse(
                type="question",
                message=decision["message"] or "Which topic would you like to look at, and how many cards?",
                suggestions=decision["suggestions"] or self._topic_suggestions(),
                plan=HorizonPlan(**self._clean(decision["plan"])) if decision["plan"] else None,
                model=model,
            )
        return HorizonChatResponse(
            type="message",
            message=decision["message"] or "I can show scored trend cards for the five topics. What would you like to see?",
            suggestions=decision["suggestions"] or self._topic_suggestions(),
            model=model,
        )

    async def _plan(self, req: HorizonChatRequest, must_run: bool) -> dict | None:
        instructions = self._planner.safe_substitute(
            today=self._now().strftime("%d %B %Y"),
            topics="\n".join(f"- {t.slug}: {t.name}" for t in self._topics),
            state=self._state_text(),
            max_cards=self._settings.horizon_cards_per_topic,
            must_run=(
                'You have already asked enough questions. You MUST answer with action "run" now, '
                "using defaults for anything still unknown."
                if must_run
                else "Ask at most one question at a time."
            ),
        )
        messages = [{"role": m.role, "content": m.content} for m in req.history]
        messages.append({"role": "user", "content": req.message})
        kwargs = {
            "model": self._settings.chat_model,
            "instructions": instructions,
            "input": messages,
            "max_output_tokens": 800,
            "store": False,
        }
        if self._settings.reasoning:
            kwargs["reasoning"] = self._settings.reasoning
        resp = await self._client.responses.create(**kwargs)
        return parse_plan(resp.output_text)

    def _state_text(self) -> str:
        lines = []
        for topic in self._topics:
            state = self._state[topic.slug]
            lines.append(
                f"- {topic.slug} ({topic.name}): {len(self._trends.stored_items(topic.slug))} research items, "
                f"{len(state.cards)} trend cards, last analysed {state.last_run or 'never'}"
            )
        return "\n".join(lines)

    def _clean(self, raw: dict | None) -> dict:
        return clean_plan(raw, [t.slug for t in self._topics], self._settings.horizon_cards_per_topic)

    def _topic_suggestions(self) -> list[str]:
        return ["All five topics", *[t.name for t in self._topics[:3]]]

    async def _execute(self, decision: dict) -> HorizonChatResponse:
        plan = HorizonPlan(**self._clean(decision["plan"]))
        model = self._settings.chat_model

        empty, too_old = [], []
        for t in self._select(plan.topics):
            if self._eligible_items(t.slug) or self._state[t.slug].cards:
                continue
            (too_old if self._trends.stored_items(t.slug) else empty).append(t.name)
        no_research = empty + too_old
        if len(no_research) == len(plan.topics):
            months = self._settings.horizon_max_age_months
            parts = []
            if empty:
                parts.append(
                    "There is no research collected yet for " + ", ".join(empty)
                    + ". The research feed has to run first (GET /trends?refresh=true, or wait for "
                    "the weekly refresh)."
                )
            if too_old:
                parts.append(
                    "All the collected research for " + ", ".join(too_old)
                    + f" is older than {months} months, so there is nothing recent to analyse. "
                    "Refresh the research feed, or raise HORIZON_MAX_AGE_MONTHS."
                )
            return HorizonChatResponse(
                type="message", message=" ".join(parts) + " Then ask me again.", plan=plan, model=model
            )

        result = await self.run(plan.topics, force=plan.reanalyze, top_n=plan.top_n, rank_by=plan.rank_by)
        return HorizonChatResponse(
            type="result",
            message=self._recap(plan, result, no_research, decision["message"]),
            suggestions=self._follow_ups(plan),
            plan=plan,
            result=result,
            model=model,
        )

    def _recap(self, plan: HorizonPlan, result: HorizonResponse, no_research: list[str], said: str) -> str:
        # no_research = topics with nothing recent to analyse (no items, or all too old)
        names = [t.name for t in result.topics]
        scope = "all five topics" if len(names) == len(self._topics) else ", ".join(names)
        count = f"the top {plan.top_n} per topic" if plan.top_n else "all trend cards"
        text = (
            f"Here are {count} for {scope}, ranked by {RANK_TEXT[plan.rank_by]} "
            f"({result.total} card{'s' if result.total != 1 else ''})."
        )
        if no_research:
            text += " Nothing recent to analyse yet for: " + ", ".join(no_research) + "."
        return text

    @staticmethod
    def _follow_ups(plan: HorizonPlan) -> list[str]:
        options = {
            "impact": "Rank by impact instead",
            "time_to_adopt": "Rank by how soon we can adopt",
            "relevance": "Rank by relevance instead",
        }
        out = [text for key, text in options.items() if key != plan.rank_by]
        if plan.top_n:
            out.append("Show all cards")
        return out[:3]

    # -- answering ---------------------------------------------------------

    def _select(self, slugs: list[str] | None) -> list[Topic]:
        if not slugs:
            return list(self._topics)
        by_slug = {t.slug: t for t in self._topics}
        chosen = []
        for slug in slugs:
            key = (slug or "").strip().lower()
            if key not in by_slug:
                raise AppError(
                    "unknown_topic",
                    f"Unknown topic '{slug}'. Valid topics: " + ", ".join(by_slug),
                    404,
                )
            if by_slug[key] not in chosen:
                chosen.append(by_slug[key])
        return chosen

    def _response(self, selected: list[Topic], top_n: int | None, rank_by: str) -> HorizonResponse:
        results: list[HorizonTopicResult] = []
        for topic in selected:
            state = self._state[topic.slug]
            ranked = rank_cards(state.cards, rank_by)
            shown = ranked[:top_n] if top_n else ranked
            warning = None
            if state.last_error:
                warning = (
                    "The last analysis failed; showing earlier cards."
                    if state.cards
                    else "The last analysis failed and there are no earlier cards."
                )
            elif not state.cards:
                if not self._trends.stored_items(topic.slug):
                    warning = "No trend cards yet. The research feed has no items for this topic."
                elif not self._eligible_items(topic.slug):
                    warning = (
                        f"No trend cards yet. All the research for this topic is older than "
                        f"{self._settings.horizon_max_age_months} months. Refresh the research feed, "
                        "or raise HORIZON_MAX_AGE_MONTHS."
                    )
                else:
                    warning = "No trend cards yet. The analysis has not run for this topic."
            results.append(
                HorizonTopicResult(
                    topic=topic.slug,
                    name=topic.name,
                    count=len(shown),
                    total_available=len(state.cards),
                    items_analyzed=state.items_analyzed,
                    last_run=state.last_run,
                    cards=shown,
                    warning=warning,
                )
            )
        runs = [r.last_run for r in results if r.last_run]
        problems = [r.name for r in results if r.warning]
        return HorizonResponse(
            topics=results,
            total=sum(r.count for r in results),
            rank_by=rank_by,  # type: ignore[arg-type]
            generated_at=max(runs) if runs else self._now().isoformat(timespec="seconds"),
            model=self._settings.horizon_model,
            warning=("Some topics have a warning: " + ", ".join(problems)) if problems else None,
        )

    # -- saving ------------------------------------------------------------

    def status(self) -> dict:
        return {
            slug: {"cards": len(s.cards), "last_run": s.last_run, "items_analyzed": s.items_analyzed}
            for slug, s in self._state.items()
        }

    def _load(self) -> None:
        if not self._store:
            return
        for slug, data in (self._store.load().get("topics") or {}).items():
            if slug not in self._state:
                continue
            try:
                self._state[slug] = HorizonTopicState.model_validate(data)
            except Exception:  # noqa: BLE001 - a damaged entry must not stop the server
                logger.warning("Ignoring damaged saved agent data for topic %s", slug)

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
