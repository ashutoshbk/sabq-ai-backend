"""Improvement Portfolio (Agent 6): scores an opportunity, checks overlap, drafts a charter.

The model reads the opportunity and writes the scores and the charter. Code checks that overlapping
initiatives exist, computes the priority score from demo weights and decides Adopt / Defer / Merge / Reject,
and builds the rationale from the numbers. The committee decides (the backend developer's approval flow).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import load_prompt
from app.core.store import JsonFileStore
from app.schemas.portfolio import CRITERIA, Charter, Evaluation, Opportunity, PortfolioItem
from app.services.agent_common import Saved, ask_json, load_platform, load_resource, short_id
from app.services.portfolio_parsing import parse_evaluation, priority_score, rationale, recommend
from app.services.trends_parsing import shorten


class PortfolioService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        store: JsonFileStore | None = None,
        platform: dict | None = None,
        rules: dict | None = None,
        best_practices=None,  # anything with gaps() and get(gap_id): normally BestPracticesService
        horizon=None,  # anything with get_cards(topic, top_n, rank_by): normally HorizonService
        topics: list | None = None,
        template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        platform = platform if platform is not None else load_platform()
        self._items = platform.get("portfolio", [])
        self._objectives = platform.get("objectives", [])
        self._rules = rules if rules is not None else load_resource("portfolio_rules.json")
        self._bp, self._horizon, self._topics = best_practices, horizon, topics or []
        self._prompt = Template(template or load_prompt("portfolio_evaluate.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._saved = Saved(store)
        self._lock = asyncio.Lock()

    # -- what is in the portfolio, and what could go in -----------------------------------------

    def items(self) -> list[PortfolioItem]:
        return [
            PortfolioItem(
                code=i["code"], type=i["type"], priority=i["priority"], title=i["title"], department=i["department"],
                due=i.get("due"), status=i["status"], execution=i.get("execution", ""), progress=i.get("progress", 0),
            )
            for i in self._items
        ]

    def opportunities(self) -> list[Opportunity]:
        """Saved practice cards (Agent 3) and the top trend card of each topic (Agent 1)."""
        out: list[Opportunity] = []
        try:
            for gap in self._bp.gaps() if self._bp else []:
                if gap.practices_count:
                    for p in self._bp.get(gap.id).practices:
                        out.append(self._mark(Opportunity(
                            id=f"practice:{gap.id}:{p.id}", source="Best Practices", title=f"{p.title} ({p.organization})",
                            description=f"{p.what_they_did} Result: {p.result} Adaptation for Dubai Police: {p.adaptation_for_dubai_police}",
                        )))
        except Exception:  # noqa: BLE001 - a problem in another agent must not break this one
            pass
        for topic in self._topics if self._horizon else []:
            try:
                cards = self._horizon.get_cards(topic.slug, None, "relevance").topics[0].cards
            except Exception:  # noqa: BLE001
                continue
            if cards:
                c = cards[0]
                out.append(self._mark(Opportunity(
                    id=f"trend:{topic.slug}", source="Horizon Scan", title=c.trend,
                    description=f"{c.summary} So what for Dubai Police: {c.so_what_for_dubai_police}",
                )))
        return out

    def _mark(self, opp: Opportunity) -> Opportunity:
        opp.evaluated = opp.id in self._saved.items
        return opp

    # -- reading and running ---------------------------------------------------------------------------

    def get(self, opportunity_id: str) -> Evaluation:
        saved = self._saved.items.get(opportunity_id)
        if not saved:
            raise AppError("not_evaluated", f"No evaluation saved for '{opportunity_id}'. Send POST /agents/portfolio/evaluate first.", 404)
        return Evaluation.model_validate(saved)

    def _resolve(self, opportunity_id: str | None, title: str | None, description: str | None) -> Opportunity:
        if opportunity_id:
            for opp in self.opportunities():
                if opp.id == opportunity_id:
                    return opp
            if opportunity_id in self._saved.items:
                return Evaluation.model_validate(self._saved.items[opportunity_id]).opportunity
            raise AppError("unknown_opportunity", f"Unknown opportunity '{opportunity_id}'. Use an id from GET /agents/portfolio/opportunities, or send a title and description.", 404)
        t, d = shorten(title, 160), shorten(description, 2000)
        if len(t) < 5 or len(d) < 20:
            raise AppError("opportunity_missing", "Send an opportunity_id, or a title and a description of a few sentences.", 422)
        return Opportunity(id="custom-" + short_id(t + d), source="Custom", title=t, description=d)

    async def evaluate(
        self, opportunity_id: str | None = None, title: str | None = None, description: str | None = None,
        refresh: bool = False,
    ) -> Evaluation:
        opp = self._resolve(opportunity_id, title, description)
        async with self._lock:
            if refresh or opp.id not in self._saved.items:
                await self._run(opp)
        return self.get(opp.id)

    async def _run(self, opp: Opportunity) -> None:
        codes = {i["code"].upper(): i["title"] for i in self._items}
        status = {i["code"].upper(): i["status"] for i in self._items}
        objective_ids = {o["id"].upper() for o in self._objectives}
        prompt = self._prompt.safe_substitute(
            title=opp.title, description=opp.description, source=opp.source,
            objectives="\n".join(f"- {o['id']}: {o['title']}" for o in self._objectives),
            portfolio="\n".join(f"- {i['code']}: {i['title']} ({i['status']}, {i['department']})" for i in self._items),
        )
        parsed = await ask_json(self._client, self._settings, prompt, lambda t: parse_evaluation(t, codes, objective_ids))

        weights = self._rules["weights"]
        thresholds = self._rules["thresholds"]
        priority = priority_score(parsed["scores"], weights)
        recommendation, merge_with = recommend(priority, parsed["overlaps"], status, thresholds)
        stamp = self._now().isoformat(timespec="seconds")
        evaluation = Evaluation(
            opportunity=opp,
            scores=[{"criterion": c, "weight": weights.get(c, 0), **parsed["scores"][c]} for c in CRITERIA],
            priority_score=priority,
            overlaps=parsed["overlaps"],
            objectives=parsed["objectives"],
            recommendation=recommendation,
            merge_with=merge_with,
            rationale=rationale(priority, recommendation, merge_with, parsed["scores"], thresholds),
            charter=Charter(**parsed["charter"]),
            last_run=stamp, generated_at=stamp, model=self._settings.agents_model,
        )
        self._saved.put(opp.id, json.loads(evaluation.model_dump_json()))

    def saved_items(self) -> dict:
        """Everything saved so far (read-only copy), used by the Executive Summary agent."""
        return dict(self._saved.items)
