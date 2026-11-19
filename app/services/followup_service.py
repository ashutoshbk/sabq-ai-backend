"""Follow-up & Progress (Agent 7): checks an owner's update against the milestones of an initiative.

The model judges whether each uploaded file supports each milestone. Code then enforces "no evidence, no
completion", computes progress, status (Green/Amber/Red/Complete), delay risk and the reminder message.
It never contacts anyone: sending the reminder is the backend developer's part.
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
from app.schemas.followup import (
    CheckRequest, Digest, DigestRow, InitiativeInfo, MilestoneInfo, Reminder, StatusCheck,
)
from app.services.agent_common import Saved, ask_json, load_platform
from app.services.followup_parsing import (
    add_lateness, build_reminder, parse_check, progress_of, rag_status, slip_risk,
)


class FollowupService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        store: JsonFileStore | None = None,
        platform: dict | None = None,
        template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        platform = platform if platform is not None else load_platform()
        # only approved initiatives with milestones can be followed up
        self._items = {
            i["code"].upper(): i for i in platform.get("portfolio", []) if i.get("status") == "approved" and i.get("milestones")
        }
        self._prompt = Template(template or load_prompt("followup_check.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._saved = Saved(store)
        self._lock = asyncio.Lock()

    def _today(self) -> date:
        return self._now().date()

    def _info(self, item: dict) -> InitiativeInfo:
        saved = self._saved.items.get(item["code"].upper())
        return InitiativeInfo(
            code=item["code"], title=item["title"], department=item["department"],
            milestones=[MilestoneInfo(**{k: m[k] for k in ("id", "title", "due")}) for m in item["milestones"]],
            platform_status=item.get("execution", ""), platform_progress=item.get("progress", 0),
            last_checked=saved.get("last_run") if saved else None,
            rag=saved.get("rag") if saved else None, progress=saved.get("progress") if saved else None,
        )

    def initiatives(self) -> list[InitiativeInfo]:
        return [self._info(i) for i in self._items.values()]

    def _item(self, code: str) -> dict:
        item = self._items.get((code or "").strip().upper())
        if not item:
            raise AppError(
                "unknown_initiative",
                f"Unknown initiative '{code}'. Only approved initiatives with milestones can be followed up: use a code from GET /agents/followup/initiatives.",
                404,
            )
        return item

    def get(self, code: str) -> StatusCheck:
        item = self._item(code)
        saved = self._saved.items.get(item["code"].upper())
        if not saved:
            raise AppError("not_checked", f"No check saved for '{item['code']}'. Send POST /agents/followup/check first.", 404)
        return StatusCheck.model_validate(saved)

    async def check(self, req: CheckRequest) -> StatusCheck:
        item = self._item(req.initiative_code)
        files = [{"name": f.name.strip(), "description": (f.description or "").strip()} for f in req.files if f.name.strip()]
        update = (req.update_text or "").strip()
        prompt = self._prompt.safe_substitute(
            title=item["title"], code=item["code"], department=item["department"],
            milestones="\n".join(f"- {m['id']}: {m['title']} (due {m['due']})" for m in item["milestones"]),
            update=update or "(no text update)",
            files="\n".join(f"- {f['name']}" + (f": {f['description']}" if f["description"] else "") for f in files) or "(no files uploaded)",
        )
        async with self._lock:
            parsed = await ask_json(
                self._client, self._settings, prompt, lambda t: parse_check(t, item["milestones"], files, update)
            )
            today = self._today()
            milestones = parsed["milestones"]
            add_lateness(milestones, today)
            progress = progress_of(milestones)
            rag, why = rag_status(milestones, today)
            risk, reasons = slip_risk(milestones, today)
            reminder = build_reminder(item, milestones)
            platform_progress = item.get("progress", 0)
            diff = None
            if abs(platform_progress - progress) >= 10:
                diff = f"The platform shows {platform_progress}% (demo data) but the evidence supports {progress}%."
            stamp = self._now().isoformat(timespec="seconds")
            result = StatusCheck(
                initiative=self._info(item),
                milestones=milestones, progress=progress, rag=rag, rag_reason=why,
                slip_risk=risk, slip_reasons=reasons, risks=parsed["risks"],
                reminder=Reminder(**reminder) if reminder else None,
                platform_progress=platform_progress, progress_difference_note=diff,
                last_run=stamp, generated_at=stamp, model=self._settings.agents_model,
            )
            self._saved.put(item["code"].upper(), json.loads(result.model_dump_json()))
        return self.get(item["code"])

    # -- the weekly digest (no model) ------------------------------------------------------------

    def digest(self) -> Digest:
        rows = []
        counts = {"Complete": 0, "Green": 0, "Amber": 0, "Red": 0, "Not checked": 0}
        for item in self._items.values():
            saved = self._saved.items.get(item["code"].upper())
            if saved:
                rag, progress = saved["rag"], saved["progress"]
                note = saved["rag_reason"]
            else:
                rag, progress, note = "Not checked", None, "No update has been checked yet."
            counts[rag] += 1
            rows.append(DigestRow(code=item["code"], title=item["title"], department=item["department"], rag=rag, progress=progress, note=note))
        needs = [r for r in rows if r.rag in ("Red", "Amber", "Not checked")]
        message = (
            f"{len(rows)} approved initiatives: " + ", ".join(f"{n} {k}" for k, n in counts.items() if n) + ". "
            + ("Needs attention: " + "; ".join(f"{r.code} ({r.rag})" for r in needs) + "." if needs else "Nothing needs attention.")
        )
        return Digest(generated_at=self._now().isoformat(timespec="seconds"), counts=counts, rows=rows, message=message)

    def saved_items(self) -> dict:
        """Everything saved so far (read-only copy), used by the Executive Summary agent."""
        return dict(self._saved.items)
