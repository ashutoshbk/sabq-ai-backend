"""Executive Summary & Data Quality (Agent 8): reads what Agents 1 to 7 saved and the demo platform data.

No model is called. Every check and every line of the brief is plain code, and each line names the
module it came from, so nothing in the brief can contradict the data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.core.config import Settings
from app.schemas.executive import BriefLine, DataQualityReport, ExecutiveBrief, ModuleStatus, QualityCheck
from app.services.agent_common import load_platform

A1, A2, A3 = "Agent 1 Horizon Scanning", "Agent 2 Benchmarking", "Agent 3 Best Practices"
A4, A5, A6, A7 = "Agent 4 Process Analyst", "Agent 5 Excellence Radar", "Agent 6 Improvement Portfolio", "Agent 7 Follow-up"
PLATFORM = "Platform demo data"
MAX_WORDS = 250


def _age_days(stamp: str | None, now: datetime) -> int | None:
    if not stamp:
        return None
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).days


class ExecutiveService:
    def __init__(
        self,
        settings: Settings,
        horizon=None, benchmark=None, best_practices=None,
        process=None, excellence=None, portfolio=None, followup=None,
        platform: dict | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ):
        self._s = settings
        self._horizon, self._benchmark, self._bp = horizon, benchmark, best_practices
        self._process, self._excellence, self._portfolio, self._followup = process, excellence, portfolio, followup
        self._platform = platform if platform is not None else load_platform()
        self._now = now_fn or (lambda: datetime.now(timezone.utc))

    # -- reading the other agents (never raises: a broken module just shows as "not run") ------------

    def _cards(self) -> list[dict]:
        out = []
        try:
            for t in self._horizon.get_cards().topics if self._horizon else []:
                out.extend({"topic": t.name, "title": c.trend, "sources": len(c.sources), "last_run": t.last_run} for c in t.cards)
        except Exception:
            pass
        return out

    def _horizon_runs(self) -> list[str]:
        try:
            return [t.last_run for t in self._horizon.get_cards().topics if t.last_run] if self._horizon else []
        except Exception:
            return []

    def _bench(self):
        try:
            res = self._benchmark.get() if self._benchmark else None
            return res if res and res.last_run else None
        except Exception:
            return None

    def _gaps(self) -> list:
        try:
            return self._bp.gaps() if self._bp else []
        except Exception:
            return []

    @staticmethod
    def _saved(service) -> dict:
        try:
            return service.saved_items() if service else {}
        except Exception:
            return {}

    # -- the modules table and the checks ------------------------------------------------------------

    def _modules(self, now: datetime) -> list[ModuleStatus]:
        bench = self._bench()
        runs = {
            A1: (max(self._horizon_runs(), default=None), len(self._cards())),
            A2: (bench.last_run if bench else None, len(bench.indicators) if bench else 0),
            A3: (max((g.last_run for g in self._gaps() if g.last_run), default=None), sum(g.practices_count for g in self._gaps())),
        }
        for name, service in ((A4, self._process), (A5, self._excellence), (A6, self._portfolio), (A7, self._followup)):
            saved = self._saved(service)
            runs[name] = (max((v.get("last_run") for v in saved.values() if v.get("last_run")), default=None), len(saved))
        out = []
        for name, (last, count) in runs.items():
            age = _age_days(last, now)
            state = "not run" if last is None else "stale" if age is not None and age > self._s.results_stale_days else "fresh"
            out.append(ModuleStatus(module=name, last_run=last, items=count, state=state))
        return out

    def data_quality(self) -> DataQualityReport:
        now = self._now()
        checks: list[QualityCheck] = []
        meta = self._platform.get("meta", {})

        updated = meta.get("last_update")
        if updated:
            try:
                y, m = (int(x) for x in updated.split("-")[:2])
                months = (now.year - y) * 12 + (now.month - m)
                if months > self._s.data_stale_months:
                    checks.append(QualityCheck(
                        code="platform_data_stale", severity="warning", module=PLATFORM,
                        message=f"The platform data was last updated in {updated}, {months} months ago (limit {self._s.data_stale_months}).",
                    ))
            except ValueError:
                checks.append(QualityCheck(code="platform_date_unreadable", severity="info", module=PLATFORM,
                                           message=f"The platform 'last update' value '{updated}' is not a YYYY-MM date."))
        for chart in meta.get("charts_without_labels", []):
            checks.append(QualityCheck(code="chart_without_labels", severity="warning", module=PLATFORM,
                                       message=f"The chart '{chart}' has no data labels, so its values cannot be read or checked."))

        bench = self._bench()
        if bench:
            for c in bench.data_checks:
                checks.append(QualityCheck(code=f"benchmark_{c.code}", severity=c.severity, module=A2, message=c.message))

        no_source = [c for c in self._cards() if c["sources"] == 0]
        if no_source:
            checks.append(QualityCheck(code="cards_without_sources", severity="warning", module=A1,
                                       message=f"{len(no_source)} trend card(s) have no source link."))

        modules = self._modules(now)
        for m in modules:
            if m.state == "stale":
                checks.append(QualityCheck(code="results_stale", severity="warning", module=m.module,
                                           message=f"Results are older than {self._s.results_stale_days} days (last run {m.last_run[:10]})."))
            elif m.state == "not run":
                checks.append(QualityCheck(code="module_not_run", severity="info", module=m.module,
                                           message="No results saved yet, so the brief has nothing from this module."))

        waiting = sum(m.items for m in modules if m.state != "not run" and m.module in (A4, A5, A6, A7))
        if waiting:
            checks.append(QualityCheck(code="drafts_waiting", severity="info", module="Agents 4 to 7",
                                       message=f"{waiting} saved result(s) are drafts waiting for a person to approve them."))

        for item in self._platform.get("portfolio", []):
            if item.get("execution") == "Delayed":
                checks.append(QualityCheck(code="initiative_delayed", severity="warning", module=PLATFORM,
                                           message=f"{item['code']} {item['title']} is marked Delayed on the platform (progress {item.get('progress', 0)}%)."))
        warnings = sum(1 for c in checks if c.severity == "warning")
        return DataQualityReport(checks=checks, warnings=warnings, modules=modules, generated_at=now.isoformat(timespec="seconds"))

    # -- the brief -----------------------------------------------------------------------------------

    def brief(self) -> ExecutiveBrief:
        now = self._now()
        changed, risks, decisions, nxt = [], [], [], []

        cards = self._cards()
        if cards:
            topics = sorted({c["topic"] for c in cards})
            changed.append(BriefLine(text=f"{len(cards)} trend cards across {len(topics)} topics are available, e.g. '{cards[0]['title']}'.", source=A1))
        bench = self._bench()
        if bench and bench.insight:
            changed.append(BriefLine(text=bench.insight.split('. ')[0].rstrip('.') + '.', source=A2))  # first sentence only: the full insight is in Agent 2
        practices = sum(g.practices_count for g in self._gaps())
        if practices:
            changed.append(BriefLine(text=f"{practices} best-practice cards are saved for the current gaps.", source=A3))
        procs = {k: v for k, v in self._saved(self._process).items() if not k.upper().startswith("SAMPLE")}  # the demo sample is not a real process
        for p in list(procs.values())[:2]:
            changed.append(BriefLine(
                text=f"{p['process']['name']}: maturity {p['maturity']['overall']} of 5, steps {p['steps_before']} to {p['steps_after']} in the proposed flow.",
                source=A4))

        excellence = sorted(self._saved(self._excellence).values(), key=lambda a: (a.get("countdown") or {}).get("days_left", 10**6))
        for a in excellence[:1]:
            cd = a.get("countdown") or {}
            line = f"{a['award']['title']}: readiness {a['readiness']}%"
            if cd:
                line += f", {cd['days_left']} days to the deadline ({cd['deadline']}{', demo date' if cd.get('deadline_is_demo') else ''})"
            risks.append(BriefLine(text=line + ".", source=A5))
            if a.get("top_gaps"):
                g = a["top_gaps"][0]
                nxt.append(BriefLine(text=f"Close the weakest criterion for {a['award']['title']}: {g['criterion']} ({g['coverage']}% covered). {g['task']}", source=A5))

        reds = [v for v in self._saved(self._followup).values() if v.get("rag") == "Red"]
        for v in reds:
            risks.append(BriefLine(text=f"{v['initiative']['code']} {v['initiative']['title']} is Red: {v['rag_reason']}", source=A7))
            decisions.append(BriefLine(text=f"Decide whether to escalate {v['initiative']['code']} to its sponsor.", source=A7))
        if not reds:
            for item in self._platform.get("portfolio", []):
                if item.get("execution") == "Delayed":
                    risks.append(BriefLine(text=f"{item['code']} {item['title']} is Delayed on the platform ({item.get('progress', 0)}% done).", source=PLATFORM))

        pending = [v for v in self._saved(self._portfolio).values() if v.get("decision") == "pending"]
        for v in sorted(pending, key=lambda v: -v["priority_score"])[:2]:
            decisions.append(BriefLine(
                text=f"Committee decision on '{v['opportunity']['title']}': recommended {v['recommendation']} (score {v['priority_score']} of 5).",
                source=A6))

        warnings = [c for c in self.data_quality().checks if c.severity == "warning"]
        if warnings:
            risks.append(BriefLine(text=f"{len(warnings)} data-quality warning(s), for example: {warnings[0].message}", source=warnings[0].module))
            nxt.append(BriefLine(text="Fix the data-quality warnings before the next review.", source="Data quality"))
        for g in self._gaps()[:1]:
            if not g.practices_count:
                nxt.append(BriefLine(text=f"Find best practices for the top gap: {g.title}.", source=A3))
        not_run = [m.module for m in self._modules(now) if m.state == "not run"]
        if not_run:
            nxt.append(BriefLine(text="Run the modules with no results yet: " + ", ".join(not_run) + ".", source="Data quality"))

        decisions = decisions[:3]
        brief = ExecutiveBrief(
            what_changed=changed or [BriefLine(text="Nothing has been run yet.", source="Data quality")],
            risks=risks, decisions_needed=decisions, next_30_days=nxt,
            word_count=0, generated_at=now.isoformat(timespec="seconds"),
        )
        brief.word_count = self._count(brief)
        # keep it to one page: drop the last lines until it fits
        while brief.word_count > MAX_WORDS:
            for section in (brief.next_30_days, brief.what_changed, brief.risks):
                if len(section) > 1:
                    section.pop()
                    break
            else:
                break
            brief.word_count = self._count(brief)
        return brief

    @staticmethod
    def _count(b: ExecutiveBrief) -> int:
        return sum(len(l.text.split()) for s in (b.what_changed, b.risks, b.decisions_needed, b.next_30_days) for l in s)
