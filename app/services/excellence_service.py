"""Excellence Radar (Agent 5): award list with deadline countdown, and a readiness assessment.

The model matches evidence to criteria. Code checks that every evidence id exists, forces coverage to 0
where nothing matches, computes the readiness average, picks the top gaps and works out the 90/60/30-day
countdown. Tasks are only suggested here: creating them after approval is the backend developer's part.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from string import Template
from typing import Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import load_prompt
from app.core.store import JsonFileStore
from app.schemas.excellence import AwardInfo, Assessment, Countdown
from app.services.agent_common import Saved, ask_json, load_platform, load_resource
from app.services.excellence_parsing import countdown, parse_assessment, parse_deadline, readiness_of, top_gaps


class ExcellenceService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        store: JsonFileStore | None = None,
        platform: dict | None = None,
        criteria: dict | None = None,
        evidence: list[dict] | None = None,
        template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        platform = platform if platform is not None else load_platform()
        self._awards = {a["id"]: a for a in platform.get("awards", [])}
        self._criteria = criteria if criteria is not None else load_resource("excellence_criteria.json")["awards"]
        self._evidence = evidence if evidence is not None else load_resource("evidence_library.json")["evidence"]
        self._prompt = Template(template or load_prompt("excellence_assess.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._saved = Saved(store)
        self._lock = asyncio.Lock()

    def _today(self) -> date:
        return self._now().date()

    # -- the award list ------------------------------------------------------------------------

    def awards(self) -> list[AwardInfo]:
        return [self._info(a) for a in self._awards.values()]

    def _info(self, award: dict, deadline: date | None = None, demo: bool | None = None) -> AwardInfo:
        if deadline is None and award.get("deadline"):
            deadline, demo = parse_deadline(award["deadline"]), bool(award.get("deadline_is_demo"))
        saved = self._saved.items.get(award["id"])
        return AwardInfo(
            id=award["id"], title=award["title"], org=award.get("org", ""), kind=award.get("kind", ""),
            priority=award.get("priority", ""), duration=award.get("duration", ""), status=award.get("status", ""),
            criteria_available=award["id"] in self._criteria,
            countdown=Countdown(**countdown(deadline, self._today(), bool(demo))) if deadline else None,
            last_assessed=saved.get("last_run") if saved else None,
            readiness=saved.get("readiness") if saved else None,
        )

    # -- reading and running -------------------------------------------------------------------

    def get(self, award_id: str) -> Assessment:
        saved = self._saved.items.get((award_id or "").strip().lower())
        if not saved:
            raise AppError("not_assessed", f"No assessment saved for '{award_id}'. Send POST /agents/excellence/assess first.", 404)
        return Assessment.model_validate(saved)

    async def assess(self, award_id: str, deadline: str | None = None, refresh: bool = False) -> Assessment:
        key = (award_id or "").strip().lower()
        award = self._awards.get(key)
        if not award:
            raise AppError("unknown_award", f"Unknown award '{award_id}'. Use an id from GET /agents/excellence/awards.", 404)
        if key not in self._criteria:
            raise AppError(
                "criteria_missing",
                f"No criteria are loaded for '{award['title']}'. Add them to app/resources/excellence_criteria.json (loaded: {', '.join(self._criteria)}).",
                422,
            )
        wanted = None
        if deadline:
            wanted = parse_deadline(deadline)
            if wanted is None:
                raise AppError("bad_deadline", "deadline must look like 2027-03-31.", 422)

        async with self._lock:
            if refresh or key not in self._saved.items:
                await self._run(award, key, wanted)
            elif wanted is not None:
                self._update_deadline(award, key, wanted)
        return self.get(key)

    def _countdown_for(self, award: dict, wanted: date | None):
        if wanted:
            return wanted, False
        if award.get("deadline"):
            return parse_deadline(award["deadline"]), bool(award.get("deadline_is_demo"))
        return None, False

    def _update_deadline(self, award: dict, key: str, wanted: date) -> None:
        """A new deadline needs no new model call: only the countdown and the task dates change."""
        saved = self._saved.items[key]
        saved["countdown"] = countdown(wanted, self._today(), False)
        saved["award"]["countdown"] = saved["countdown"]
        for task in saved["top_gaps"]:
            task["due"] = (saved["countdown"]["checkpoints"][-1]["date"])
        saved["timeline_note"] = "Countdown from the deadline you sent."
        self._saved.put(key, saved)

    async def _run(self, award: dict, key: str, wanted: date | None) -> None:
        crit_set = self._criteria[key]
        criteria = crit_set["criteria"]
        prompt = self._prompt.safe_substitute(
            award=award["title"],
            criteria="\n".join(f"- {c['id']}: {c['name']}. {c.get('desc', '')}" for c in criteria),
            evidence="\n".join(f"- {e['id']}: {e['name']} ({e.get('type', '')}). {e.get('summary', '')}" for e in self._evidence),
        )
        parsed = await ask_json(
            self._client, self._settings, prompt, lambda t: parse_assessment(t, criteria, self._evidence)
        )
        deadline, is_demo = self._countdown_for(award, wanted)
        cd = countdown(deadline, self._today(), is_demo) if deadline else None
        due = cd["checkpoints"][-1]["date"] if cd else None  # the 30-day checkpoint
        stamp = self._now().isoformat(timespec="seconds")
        readiness = readiness_of(parsed["criteria"])
        info = self._info(award, deadline, is_demo)
        assessment = Assessment(
            award=info,
            criteria_set=crit_set["name"],
            readiness=readiness,
            criteria=parsed["criteria"],
            top_gaps=top_gaps(parsed["criteria"], parsed["owners"], due),
            countdown=cd,
            timeline_note=(
                "Countdown from the deadline you sent." if wanted else
                "Countdown from the demo deadline (replace it with the real submission date)." if cd and is_demo else
                "Countdown from the stated deadline." if cd else
                "No deadline is known for this award. Send a deadline to get the 90/60/30-day countdown."
            ),
            last_run=stamp, generated_at=stamp, model=self._settings.agents_model,
        )
        self._saved.put(key, json.loads(assessment.model_dump_json()))

    def saved_items(self) -> dict:
        """Everything saved so far (read-only copy), used by the Executive Summary agent."""
        return dict(self._saved.items)
