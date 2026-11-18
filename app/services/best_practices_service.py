"""Best Practices Curator (Agent 3).

It finds proven practices at leading organizations for a GAP. A gap comes from:
  - Agent 1's trend cards plus the platform demo data (one gap per topic, built in plain code), or
  - the person's own words.

Honesty rules (same pattern as the other agents):
  - web search only, on our trusted domains; links are never written into the code
  - a practice is kept only if its page was cited by the search tool, is on a trusted domain and
    states a measurable result; otherwise there is no card ("no evidence found" is a normal answer)
  - source names come from our domain list; ranking and the recommendation are computed in code
  - the platform data used for the fit check is demo data and is labelled as such

It works on demand as a short conversation, and the saved practices are reused until the person
asks to search again.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import RESOURCES_DIR, DomainEntry, Topic, load_prompt
from app.core.store import JsonFileStore
from app.schemas.best_practices import (
    BestPracticesResponse,
    Gap,
    GapsResponse,
    Practice,
    PracticeChatRequest,
    PracticeChatResponse,
    PracticePlan,
    StoredGap,
)
from app.services.best_practices_parsing import (
    build_recommendation,
    clean_custom_gap,
    clean_plan,
    custom_gap_id,
    drop_repeated_results,
    fingerprint,
    parse_plan,
    parse_practices,
    rank_key,
)
from app.services.horizon_service import describe_context, load_context

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # a second try only if the first answer could not be read
STORE_VERSION = 1
CARDS_IN_GAP = 3  # Agent 1 trend cards named in a gap
NO_PRACTICE_WARNING = (
    "No practice with a published, measurable result was found on the trusted sources for this gap."
)


def load_best_practice_domains() -> list[DomainEntry]:
    data = json.loads((RESOURCES_DIR / "best_practice_domains.json").read_text(encoding="utf-8"))
    return [DomainEntry(**item) for item in data]


class BestPracticesService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        horizon,  # anything with get_cards(topic, top_n, rank_by): normally HorizonService
        topics: list[Topic],
        domains: list[DomainEntry],
        store: JsonFileStore | None = None,
        context: dict[str, dict] | None = None,
        search_template: str | None = None,
        planner_template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._horizon = horizon
        self._topics = topics
        self._domains = domains
        self._store = store
        self._context = load_context() if context is None else context
        self._search_prompt = Template(search_template or load_prompt("best_practices_search.md"))
        self._planner = Template(planner_template or load_prompt("best_practices_planner.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._saved: dict[str, StoredGap] = {}
        self._warnings: dict[str, str | None] = {}  # in memory only: why the last search had no new result
        self._lock = asyncio.Lock()
        self._load()

    # -- the gaps ----------------------------------------------------------

    def gaps(self) -> list[Gap]:
        """Topic gaps (from Agent 1 and the platform data), most pressing first, then typed gaps."""
        topic_gaps = [g for g in (self._topic_gap(t) for t in self._topics) if g]
        topic_gaps.sort(
            key=lambda g: (
                -(g.strategic_gap_percent if g.strategic_gap_percent is not None else -1),
                -(g.priority or 0),
            )
        )
        for position, gap in enumerate(topic_gaps, start=1):
            gap.priority = position
        typed = sorted(
            (s for s in self._saved.values() if s.gap.origin == "custom"),
            key=lambda s: s.last_run or "",
            reverse=True,
        )
        out = topic_gaps + [self._with_state(s.gap) for s in typed]
        return [self._with_state(g) for g in out]

    def _with_state(self, gap: Gap) -> Gap:
        saved = self._saved.get(gap.id)
        gap.practices_count = len(saved.practices) if saved else 0
        gap.last_run = saved.last_run if saved else None
        return gap

    def _topic_gap(self, topic: Topic) -> Gap | None:
        cards = []
        try:
            cards = self._horizon.get_cards(topic.slug, None, "relevance").topics[0].cards
        except Exception:  # noqa: BLE001 - a problem in Agent 1 must not break this agent
            logger.warning("Could not read trend cards for %s", topic.slug)
        profile = self._context.get(topic.slug)
        if not cards and not profile:
            return None

        top = cards[:CARDS_IN_GAP]
        parts = [
            f"Dubai Police wants to keep pace with leading organizations on {topic.name} ({topic.focus.strip().rstrip('.')})."
        ]
        if top:
            parts.append(
                "Recent trends found by the Horizon Scanning agent: "
                + "; ".join(f"{c.trend} (so what for us: {c.so_what_for_dubai_police})" for c in top)
            )
        context_text = None
        if profile:
            context_text = "Platform demo data. " + describe_context(profile)
            parts.append(f"Close the gap of {profile.get('strategic_gap_percent', 'n/a')}% shown on the platform.")
        description = " ".join(parts)

        best = max((c.scores.relevance.score for c in cards), default=0)
        return Gap(
            id=topic.slug,
            title=f"{topic.name}: how leading organizations close the gap",
            description=description,
            origin="trend",
            topic=topic.slug,
            topic_name=topic.name,
            based_on=[c.trend for c in top],
            platform_context=context_text,
            strategic_gap_percent=(profile or {}).get("strategic_gap_percent"),
            priority=best,
        )

    def _find_gap(self, gap_id: str | None, custom_text: str | None) -> Gap:
        if gap_id:
            key = gap_id.strip().lower()
            for gap in self.gaps():
                if gap.id == key:
                    return gap
            raise AppError(
                "unknown_gap",
                f"Unknown gap '{gap_id}'. Use an id from GET /agents/best-practices/gaps, or send custom_gap.",
                404,
            )
        text = clean_custom_gap(custom_text)
        if text is None:
            raise AppError(
                "gap_missing",
                "Send a gap_id, or describe the gap in custom_gap (at least a few words).",
                422,
            )
        return Gap(id=custom_gap_id(text), title=text, description=text, origin="custom")

    # -- reading and running --------------------------------------------------

    def get(self, gap_id: str) -> BestPracticesResponse:
        """The saved practices for a gap. Never searches."""
        return self._response(self._find_gap(gap_id, None))

    async def run(
        self, gap_id: str | None = None, custom_gap: str | None = None, refresh: bool = False
    ) -> BestPracticesResponse:
        """Search for practices unless some are already saved (or the person asks to search again)."""
        gap = self._find_gap(gap_id, custom_gap)
        async with self._lock:
            saved = self._saved.get(gap.id)
            if refresh or not saved or not saved.last_run:
                await self._search_and_save(gap)
        return self._response(gap)

    async def _search_and_save(self, gap: Gap) -> None:
        stamp = self._now().isoformat(timespec="seconds")
        new = await self._search(gap)
        old = self._saved.get(gap.id)

        warning = None
        if new:
            practices = self._build_cards(new, stamp)
        elif old and old.practices:
            practices = old.practices  # an empty search never wipes earlier results
            warning = "The new search found no practice with a published result, so the earlier results are shown."
        else:
            practices = []
            warning = NO_PRACTICE_WARNING

        self._saved[gap.id] = StoredGap(
            gap=gap, practices=practices, last_run=stamp, basis=fingerprint(gap.description)
        )
        self._warnings[gap.id] = warning
        self._trim_custom()
        self._save()

    def _build_cards(self, found: list[dict], stamp: str) -> list[Practice]:
        ordered = drop_repeated_results(sorted(found, key=rank_key))[: self._settings.best_practices_max_cards]
        return [
            Practice(id=uuid.uuid4().hex[:10], rank=i, generated_at=stamp, **item)
            for i, item in enumerate(ordered, start=1)
        ]

    async def _search(self, gap: Gap) -> list[dict]:
        prompt = self._search_prompt.safe_substitute(
            today=self._now().strftime("%d %B %Y"),
            gap_title=gap.title,
            gap_description=gap.description if gap.description != gap.title else "",
            process_context=gap.platform_context or "None available for this gap.",
            max_practices=self._settings.best_practices_max_cards,
        )
        kwargs = {
            "model": self._settings.best_practices_model,
            "input": prompt,
            "tools": [{"type": "web_search", "filters": {"allowed_domains": [d.domain for d in self._domains]}}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": self._settings.best_practices_max_output_tokens,
            "store": False,
        }
        if self._settings.reasoning:
            kwargs["reasoning"] = self._settings.reasoning

        for attempt in range(1, MAX_ATTEMPTS + 1):
            resp = await self._client.responses.create(**kwargs)
            parsed = parse_practices(resp.output_text, resp.model_dump(), self._domains, self._now().date())
            if parsed is not None:
                return parsed
            logger.warning("Best practices answer unreadable (attempt %d of %d)", attempt, MAX_ATTEMPTS)
        raise AppError(
            "no_valid_results", "The search returned an answer that could not be read. Please try again.", 502
        )

    def _trim_custom(self) -> None:
        typed = sorted(
            (s for s in self._saved.values() if s.gap.origin == "custom"),
            key=lambda s: s.last_run or "",
            reverse=True,
        )
        for extra in typed[self._settings.best_practices_max_custom_gaps :]:
            self._saved.pop(extra.gap.id, None)

    # -- answering -------------------------------------------------------------

    def _response(self, gap: Gap) -> BestPracticesResponse:
        saved = self._saved.get(gap.id)
        gap = self._with_state(gap)
        practices = saved.practices if saved else []
        outdated = bool(saved and saved.basis and saved.basis != fingerprint(gap.description))
        warning = self._warnings.get(gap.id)
        if not saved or not saved.last_run:
            warning = "No search has been run for this gap yet. Use refresh=true or ask in the chat."
        elif warning is None and not practices:
            warning = NO_PRACTICE_WARNING
        if outdated:
            note = "Agent 1's trend cards or the platform data changed since this search. Search again to update."
            warning = f"{warning} {note}" if warning else note
        return BestPracticesResponse(
            gap=gap,
            practices=practices,
            recommendation=build_recommendation(practices, gap.title),
            outdated=outdated,
            last_run=saved.last_run if saved else None,
            generated_at=self._now().isoformat(timespec="seconds"),
            model=self._settings.best_practices_model,
            warning=warning,
        )

    # -- the on-demand conversation ----------------------------------------------

    async def chat(self, req: PracticeChatRequest) -> PracticeChatResponse:
        asked = sum(1 for m in req.history if m.role == "assistant")
        must_run = asked >= self._settings.best_practices_max_questions
        model = self._settings.chat_model
        gaps = self.gaps()

        if not gaps:
            return PracticeChatResponse(
                type="message",
                message=(
                    "There are no gaps from the trend cards or platform data yet. Describe the gap in your "
                    "own words (for example: 'speed up approval of public-safety permits') and I will search."
                ),
                model=model,
            )

        decision = await self._plan(req, gaps, must_run)
        if decision is None:
            if not must_run:
                return PracticeChatResponse(
                    type="question",
                    message="Sorry, I did not catch that. Which gap would you like practices for?",
                    suggestions=self._suggestions(gaps),
                    model=model,
                )
            decision = {"action": "run", "message": "", "suggestions": [], "plan": None}

        action = decision["action"]
        if action == "run" or (must_run and action == "ask"):
            return await self._execute(decision, gaps)
        if action == "ask":
            return PracticeChatResponse(
                type="question",
                message=decision["message"] or "Which gap would you like practices for?",
                suggestions=decision["suggestions"] or self._suggestions(gaps),
                model=model,
            )
        return PracticeChatResponse(
            type="message",
            message=decision["message"]
            or "I can find proven practices, with measurable results, for a gap. Which gap would you like?",
            suggestions=decision["suggestions"] or self._suggestions(gaps),
            model=model,
        )

    async def _plan(self, req: PracticeChatRequest, gaps: list[Gap], must_run: bool) -> dict | None:
        instructions = self._planner.safe_substitute(
            today=self._now().strftime("%d %B %Y"),
            max_practices=self._settings.best_practices_max_cards,
            gaps="\n".join(
                f"- {g.id}: {g.title} ({g.practices_count} saved practices)" for g in gaps
            ),
            must_run=(
                'You have already asked enough questions. You MUST answer with action "run" now.'
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

    @staticmethod
    def _suggestions(gaps: list[Gap]) -> list[str]:
        names = [g.topic_name or g.title for g in gaps if g.origin == "trend"][:3]
        return names or ["Describe the gap in my own words"]

    async def _execute(self, decision: dict, gaps: list[Gap]) -> PracticeChatResponse:
        model = self._settings.chat_model
        plan = PracticePlan(**clean_plan(decision["plan"], [g.id for g in gaps]))
        defaulted = False
        if not plan.gap_id and not plan.custom_gap:
            plan.gap_id = gaps[0].id  # nothing clear: start with the most pressing gap, and say so
            defaulted = True

        result = await self.run(plan.gap_id, plan.custom_gap, refresh=plan.refresh)
        n = len(result.practices)
        text = (
            f"Here {'is' if n == 1 else 'are'} {n} practice{'s' if n != 1 else ''} for “{result.gap.title}”, "
            "ranked by strength of evidence, achieved before estimated results, then effort, then recency."
            if n
            else f"I found no practice with a published, measurable result for “{result.gap.title}” on the trusted sources."
        )
        if defaulted:
            text = "I started with the most pressing gap because none was specified. " + text
        return PracticeChatResponse(
            type="result",
            message=text,
            suggestions=[f"Practices for {g.topic_name}" for g in gaps if g.topic_name and g.id != result.gap.id][:3],
            plan=plan,
            result=result,
            model=model,
        )

    # -- saving ------------------------------------------------------------------

    def _load(self) -> None:
        if not self._store:
            return
        data = self._store.load().get("gaps")
        if not isinstance(data, dict):
            return
        for gap_id, raw in data.items():
            try:
                self._saved[gap_id] = StoredGap.model_validate(raw)
            except Exception:  # noqa: BLE001 - a damaged entry must not stop the server
                logger.warning("Ignoring damaged saved best-practice data for %s", gap_id)

    def _save(self) -> None:
        if not self._store:
            return
        self._store.save(
            {
                "version": STORE_VERSION,
                "saved_at": self._now().isoformat(timespec="seconds"),
                "gaps": {k: v.model_dump() for k, v in self._saved.items()},
            }
        )

    def status(self) -> dict:
        return {"gaps_saved": len(self._saved), "practices": sum(len(s.practices) for s in self._saved.values())}
