"""Process & Procedure Analyst (Agent 4).

Reads SOP text and returns steps, waste, a maturity score per rubric dimension and a simpler flow.
The model reads the text. Code checks that every quoted line really appears in the SOP, computes the
maturity average and the step counts, and lowers any score that has no supporting quote.
Nothing here updates a platform tile: the process owner approves first (the backend developer's part).
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
from app.schemas.process import ProcessAnalysis, ProcessInfo, ProcessRef
from app.services.agent_common import Saved, ask_json, load_platform, load_resource, make_store, short_id
from app.services.process_parsing import parse_analysis
from app.services.trends_parsing import shorten

MAX_SOP_CHARS = 12000


class ProcessService:
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: Settings,
        store: JsonFileStore | None = None,
        platform: dict | None = None,
        sops: list[dict] | None = None,
        rubric: list[dict] | None = None,
        template: str | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._client = client
        self._settings = settings
        self._platform = platform if platform is not None else load_platform()
        self._sops = {s["id"]: s for s in (sops if sops is not None else load_resource("sop_demo.json")["sops"])}
        self._rubric = rubric if rubric is not None else load_resource("maturity_rubric.json")["dimensions"]
        self._prompt = Template(template or load_prompt("process_analysis.md"))
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._saved = Saved(store)
        self._lock = asyncio.Lock()

    # -- what can be analysed ----------------------------------------------------------------

    def processes(self) -> list[ProcessInfo]:
        """The platform's processes, then SOPs that are not one of them (such as the sample)."""
        out = []
        for p in self._platform.get("processes", []):
            out.append(self._info(p["code"], p["name"], p.get("maturity")))
        known = {i.code for i in out}
        for sop in self._sops.values():
            if sop["id"] not in known:
                out.append(self._info(sop["id"], sop["name"], None))
        return out

    def _info(self, code: str, name: str, maturity) -> ProcessInfo:
        saved = self._saved.items.get(code)
        return ProcessInfo(
            code=code, name=name, demo_maturity=maturity, sop_available=code in self._sops,
            analyzed_at=saved.get("last_run") if saved else None,
        )

    # -- reading and running -------------------------------------------------------------------

    def get(self, code: str) -> ProcessAnalysis:
        saved = self._saved.items.get(self._key(code))
        if not saved:
            raise AppError("not_analyzed", f"No analysis saved for '{code}'. Send POST /agents/process/analyze first.", 404)
        return ProcessAnalysis.model_validate(saved)

    @staticmethod
    def _key(code: str) -> str:
        return (code or "").strip().upper() if not (code or "").startswith("custom-") else code

    async def analyze(
        self, process_code: str | None = None, sop_text: str | None = None, title: str | None = None,
        refresh: bool = False,
    ) -> ProcessAnalysis:
        if process_code:
            key = self._key(process_code)
            sop = self._sops.get(key)
            known = {p["code"].upper() for p in self._platform.get("processes", [])}
            if not sop and key not in known:
                raise AppError("unknown_process", f"Unknown process '{process_code}'. Use a code from GET /agents/process/processes, or send sop_text.", 404)
            if not sop:
                raise AppError("sop_missing", f"No SOP text is available for '{process_code}'. Send sop_text.", 422)
            text, name, origin = sop["text"], sop["name"], sop.get("origin", "Demo SOP")
            code = key
        else:
            text = (sop_text or "").strip()
            if len(text) < 40:
                raise AppError("sop_missing", "Send a process_code, or paste the SOP in sop_text (at least a few sentences).", 422)
            name = shorten(title, 120) or "Pasted SOP"
            code, origin = "custom-" + short_id(text), "Pasted by the user"

        async with self._lock:
            if refresh or code not in self._saved.items:
                await self._run(code, name, origin, text[:MAX_SOP_CHARS])
        return self.get(code)

    async def _run(self, code: str, name: str, origin: str, sop_text: str) -> None:
        prompt = self._prompt.safe_substitute(
            name=name,
            sop=sop_text,
            rubric="\n".join(
                f"- {d['id']} ({d['name']}): " + "; ".join(f"{k} = {v}" for k, v in d["levels"].items())
                for d in self._rubric
            ),
        )
        parsed = await ask_json(
            self._client, self._settings, prompt, lambda t: parse_analysis(t, sop_text, self._rubric)
        )
        platform = next((p for p in self._platform.get("processes", []) if p["code"] == code), None)
        stamp = self._now().isoformat(timespec="seconds")
        analysis = ProcessAnalysis(
            process=ProcessRef(code=code, name=name, origin=origin),
            platform_maturity=platform.get("maturity") if platform else None,
            last_run=stamp,
            generated_at=stamp,
            model=self._settings.agents_model,
            **parsed,
        )
        self._saved.put(code, json.loads(analysis.model_dump_json()))

    def saved_items(self) -> dict:
        """Everything saved so far (read-only copy), used by the Executive Summary agent."""
        return dict(self._saved.items)
