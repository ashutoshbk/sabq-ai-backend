"""Benchmarking Agent.

It searches the web (only the trusted domains of each indicator) for PUBLISHED figures about the
entities of the platform's competitive table, and shows what it found, with the source link and
a plain explanation of what can and cannot be compared.

What is hard-coded: NOTHING about links. The entities and the indicators (name, publisher,
what to look for, trusted domains) live in app/resources/*.json. The pages are found by search.

Honesty rules (the same spirit as the research feed):
  - a figure is kept only if the search tool actually cited its page, the page is on the
    indicator's trusted domains, and the number fits its own stated scale
  - not found is a normal answer; nothing is estimated or filled in
  - ranking, normalizing, comparability and the written insight are all computed in plain code;
    the model only reads pages
  - entities are ranked together only for the same indicator, the same year and the same basis
  - the platform's own table scores are demo data and are never ranked against published figures
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import RESOURCES_DIR, load_prompt
from app.core.store import JsonFileStore
from app.schemas.benchmark import (
    Baseline,
    BenchmarkEntity,
    BenchmarkIndicator,
    BenchmarkResponse,
    BenchmarkState,
    DataCheck,
    EntityFinding,
    EntityResult,
    IndicatorResult,
    RankedSubject,
    StoredFinding,
)
from app.services.benchmark_parsing import (
    Group,
    build_group,
    build_insight,
    check_baseline,
    parse_findings,
)
from app.services.trends_parsing import shorten

logger = logging.getLogger(__name__)

STORE_VERSION = 1
COUNTRY_NOTE = " This figure describes the country as a whole, not this organization."
NOT_FOUND_NOTE = "No published figure was found on the trusted sources for this indicator."


def load_entities() -> tuple[list[BenchmarkEntity], list[str]]:
    data = json.loads((RESOURCES_DIR / "benchmark_entities.json").read_text(encoding="utf-8"))
    return [BenchmarkEntity(**e) for e in data["entities"]], list(data.get("valid_types") or [])


def load_indicators() -> list[BenchmarkIndicator]:
    data = json.loads((RESOURCES_DIR / "benchmark_indicators.json").read_text(encoding="utf-8"))
    return [BenchmarkIndicator(**i) for i in data]


def label_of(entity: BenchmarkEntity) -> str:
    """The short name used when searching, for example 'Dubai Police'."""
    return entity.name.split("·")[-1].strip()


class BenchmarkService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        entities: list[BenchmarkEntity],
        valid_types: list[str],
        indicators: list[BenchmarkIndicator],
        store: JsonFileStore | None = None,
        search_template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._entities = entities
        self._valid_types = valid_types
        self._indicators = indicators
        self._store = store
        self._search_prompt = Template(search_template or load_prompt("benchmark_search.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._state = BenchmarkState()
        self._errors: list[str] = []  # in memory only: which searches failed last time
        self._lock = asyncio.Lock()
        self._load()

    # -- public ------------------------------------------------------------

    def get(self) -> BenchmarkResponse:
        """The saved results. Never searches."""
        return self._response()

    def indicators(self) -> list[BenchmarkIndicator]:
        return list(self._indicators)

    async def run(self, force: bool = False) -> BenchmarkResponse:
        """Search and check everything again. Without force, only if the results are due."""
        async with self._lock:
            if force or self._due():
                await self._run_locked()
        return self._response()

    # -- running -----------------------------------------------------------

    def _due(self) -> bool:
        if not self._state.last_run:
            return True
        have = {f.indicator_id for f in self._state.findings}
        if any(i.id not in have for i in self._indicators):  # an earlier search failed
            return True
        try:
            last = datetime.fromisoformat(self._state.last_run)
        except ValueError:
            return True
        return self._now() - last >= timedelta(days=self._settings.benchmark_refresh_days)

    def _tasks(self) -> list[tuple[BenchmarkIndicator, dict[str, str]]]:
        """One search per indicator. Each maps what the model is asked about -> the key we store it under."""
        tasks = []
        for indicator in self._indicators:
            if indicator.level == "country":
                subjects = {c: c for c in sorted({e.country for e in self._entities})}
            else:
                subjects = {
                    label_of(e): e.id for e in self._entities if e.org_kind in indicator.applies_to_kinds
                }
            if subjects:
                tasks.append((indicator, subjects))
        return tasks

    async def _run_locked(self) -> None:
        tasks = self._tasks()
        semaphore = asyncio.Semaphore(self._settings.trends_search_concurrency)

        async def one(indicator: BenchmarkIndicator, subjects: dict[str, str]) -> dict[str, dict]:
            async with semaphore:
                return await self._search(indicator, subjects)

        results = await asyncio.gather(*(one(i, s) for i, s in tasks), return_exceptions=True)
        stamp = self._now().isoformat(timespec="seconds")
        self._errors = []
        failures: list[Exception] = []

        for (indicator, subjects), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.warning("Benchmark search for %s failed: %s", indicator.id, result)
                self._errors.append(f"{indicator.name}: {type(result).__name__}")
                failures.append(result)
                continue
            if isinstance(result, BaseException):
                raise result
            self._merge(indicator, subjects, result, stamp)

        if failures and len(failures) == len(tasks):
            raise failures[0]

        self._state.last_run = stamp
        self._save()

    async def _search(self, indicator: BenchmarkIndicator, subjects: dict[str, str]) -> dict[str, dict]:
        prompt = self._search_prompt.safe_substitute(
            today=self._now().strftime("%d %B %Y"),
            indicator=indicator.name,
            publisher=indicator.publisher,
            hint=indicator.hint,
            subjects="\n".join(f"- {name}" for name in subjects),
        )
        kwargs = {
            "model": self._settings.benchmark_model,
            "input": prompt,
            "tools": [{"type": "web_search", "filters": {"allowed_domains": indicator.domains}}],
            "tool_choice": "required",
            "include": ["web_search_call.action.sources"],
            "max_output_tokens": self._settings.benchmark_max_output_tokens,
            "store": False,
        }
        if self._settings.reasoning:
            kwargs["reasoning"] = self._settings.reasoning

        resp = await self._client.responses.create(**kwargs)
        parsed = parse_findings(
            resp.output_text, resp.model_dump(), list(subjects), indicator, self._now().date()
        )
        if parsed is None:
            raise AppError(
                "no_valid_results",
                f"The search for '{indicator.name}' returned an answer that could not be read.",
                502,
            )
        return {subjects[name]: {**finding, "subject": subjects[name]} for name, finding in parsed.items()}

    def _merge(
        self, indicator: BenchmarkIndicator, subjects: dict[str, str], found: dict[str, dict], stamp: str
    ) -> None:
        """New figures replace old ones. 'Not found' never wipes a figure we already have."""
        existing = {(f.indicator_id, f.subject): f for f in self._state.findings}
        for key in subjects.values():
            old = existing.get((indicator.id, key))
            if key in found:
                new = StoredFinding(**found[key], checked_at=stamp)
                if old and old.status == "found" and old.year and new.year and new.year < old.year:
                    continue  # keep the newer edition we already have
            else:
                if old and old.status == "found":
                    continue
                new = StoredFinding(indicator_id=indicator.id, subject=key, status="not_found", checked_at=stamp)
            existing[(indicator.id, key)] = new
        self._state.findings = list(existing.values())

    # -- answering -----------------------------------------------------------

    def _groups(self) -> dict[str, Group]:
        return {
            i.id: build_group([f for f in self._state.findings if f.indicator_id == i.id], i)
            for i in self._indicators
        }

    def _response(self) -> BenchmarkResponse:
        groups = self._groups()
        stored = {(f.indicator_id, f.subject): f for f in self._state.findings}
        by_id = {i.id: i for i in self._indicators}

        entities = []
        for e in self._entities:
            findings = []
            for ind in self._indicators:
                if ind.level == "country":
                    key, describes = e.country, f"{e.country} (the country as a whole)"
                elif e.org_kind in ind.applies_to_kinds:
                    key, describes = e.id, label_of(e)
                else:
                    continue
                f = stored.get((ind.id, key))
                if not f:
                    continue  # not collected yet
                findings.append(self._entity_finding(ind, f, describes, groups[ind.id]))
            entities.append(
                EntityResult(
                    id=e.id, name=e.name, org_kind=e.org_kind, country=e.country, scope=e.scope,
                    type=e.type, is_self=e.is_self,
                    baseline=Baseline(score=e.demo_score, rank=e.demo_rank),
                    findings=findings,
                )
            )

        indicators = [self._indicator_result(by_id[i.id], groups[i.id]) for i in self._indicators]
        warning = None
        if not self._state.last_run:
            warning = "Nothing has been collected yet. Run GET /agents/benchmark?refresh=true."
        elif self._errors:
            warning = "Some searches failed last time (earlier figures are kept): " + "; ".join(self._errors)

        data_checks = check_baseline(self._entities, self._valid_types)
        return BenchmarkResponse(
            entities=entities,
            indicators=indicators,
            data_checks=data_checks,
            insight=build_insight(indicators, entities, data_checks),
            last_run=self._state.last_run,
            generated_at=self._state.last_run or self._now().isoformat(timespec="seconds"),
            model=self._settings.benchmark_model,
            warning=warning,
        )

    def _entity_finding(self, ind: BenchmarkIndicator, f: StoredFinding, describes: str, group: Group) -> EntityFinding:
        if f.status == "not_found":
            return EntityFinding(
                indicator_id=ind.id, indicator_name=ind.name, level=ind.level, status="not_found",
                describes=describes, comparable=False, note=NOT_FOUND_NOTE,
            )
        note = group.notes.get(f.subject, "")
        if ind.level == "country":
            note += COUNTRY_NOTE
        if f.year and self._now().year - f.year >= self._settings.benchmark_stale_years:
            note += f" This figure is from {f.year}, so it may be out of date."
        return EntityFinding(
            indicator_id=ind.id, indicator_name=ind.name, level=ind.level, status="found",
            describes=describes, metric=f.metric, value=f.value, scale_min=f.scale_min,
            scale_max=f.scale_max, rank=f.rank, rank_of=f.rank_of, year=f.year,
            normalized=f.normalized if f.subject in group.ranks else None,
            source_name=f.source_name, source_url=f.source_url, evidence=f.evidence,
            confidence=f.confidence, comparable=f.subject in group.ranks,
            group_rank=group.ranks.get(f.subject), note=note.strip(),
        )

    def _indicator_result(self, ind: BenchmarkIndicator, group: Group) -> IndicatorResult:
        found = [f for f in self._state.findings if f.indicator_id == ind.id and f.status == "found"]
        ranking = [
            RankedSubject(
                subject=f.subject, value=f.value, normalized=f.normalized, rank=f.rank,
                rank_of=f.rank_of, group_rank=group.ranks[f.subject],
            )
            for f in sorted(found, key=lambda f: group.ranks.get(f.subject, 10**6))
            if f.subject in group.ranks
        ]
        if not any(f.indicator_id == ind.id for f in self._state.findings):
            note = "Not collected yet."
        elif group.compared:
            note = f"{len(ranking)} subjects ranked together: same indicator, same year ({group.year}), same basis."
        elif not ind.comparable_across_entities:
            note = (
                f"{len(found)} published figure(s) shown for information. Surveys use different "
                "methods, so they are not ranked against each other."
                if found else NOT_FOUND_NOTE
            )
        elif found:
            note = "Figures were found, but not for two or more subjects with the same year and scale, so nothing is ranked."
        else:
            note = NOT_FOUND_NOTE
        return IndicatorResult(
            id=ind.id, name=ind.name, publisher=ind.publisher, level=ind.level,
            year=group.year if group.compared else None, compared=group.compared,
            ranking=ranking, note=note,
        )

    # -- saving ----------------------------------------------------------------

    def _load(self) -> None:
        if not self._store:
            return
        data = self._store.load()
        if not data:
            return
        try:
            self._state = BenchmarkState.model_validate(
                {k: data.get(k) for k in ("findings", "last_run") if data.get(k) is not None}
            )
        except Exception:  # noqa: BLE001 - a damaged file must not stop the server
            logger.warning("Ignoring damaged saved benchmark data")
            self._state = BenchmarkState()

    def _save(self) -> None:
        if not self._store:
            return
        self._store.save(
            {"version": STORE_VERSION, "saved_at": self._now().isoformat(timespec="seconds"),
             **self._state.model_dump()}
        )
