"""Checks on the model's readiness assessment, and the deadline countdown (pure code)."""

from __future__ import annotations

from datetime import date, timedelta

from app.services.agent_common import whole_between
from app.services.horizon_parsing import load_json_object
from app.services.trends_parsing import shorten

REMINDER_DAYS = (90, 60, 30)
MAX_GAPS = 5
NO_EVIDENCE = "No evidence in the library."


def parse_assessment(text: str | None, criteria: list[dict], evidence: list[dict]) -> dict | None:
    """Checked criteria results and owner suggestions, or None if unreadable."""
    data = load_json_object(text)
    if not data or not isinstance(data.get("criteria"), list):
        return None
    ev_by_id = {e["id"].upper(): e for e in evidence}
    wanted = {c["id"].upper(): c for c in criteria}
    raw_by_id: dict[str, dict] = {}
    for raw in data["criteria"]:
        if isinstance(raw, dict):
            raw_by_id.setdefault(str(raw.get("id") or "").strip().upper(), raw)

    results = []
    for key, crit in wanted.items():
        raw = raw_by_id.get(key) or {}
        matches, seen = [], set()
        for m in raw.get("matches") or []:
            if not isinstance(m, dict):
                continue
            eid = str(m.get("evidence_id") or "").strip().upper()
            if eid in ev_by_id and eid not in seen:  # only evidence that really exists
                seen.add(eid)
                matches.append(
                    {"evidence_id": ev_by_id[eid]["id"], "evidence_name": ev_by_id[eid]["name"], "why": shorten(m.get("why"), 200) or "Matches this criterion."}
                )
        coverage = whole_between(raw.get("coverage"), 0, 100) or 0
        missing = shorten(raw.get("missing"), 300)
        if not matches:
            coverage, missing = 0, (missing if "no evidence" in missing.lower() else f"{NO_EVIDENCE} {missing}".strip())
        results.append(
            {"id": crit["id"], "name": crit["name"], "matches": matches, "coverage": coverage,
             "missing": missing or "Not stated.", "no_evidence": not matches}
        )

    owners = {}
    for raw in data.get("owner_suggestions") or []:
        if isinstance(raw, dict):
            cid = str(raw.get("criterion_id") or "").strip().upper()
            role = shorten(raw.get("owner_role"), 80)
            if cid in wanted and role:
                owners.setdefault(cid, role)
    return {"criteria": results, "owners": owners}


def readiness_of(results: list[dict]) -> float:
    return round(sum(r["coverage"] for r in results) / len(results), 1) if results else 0.0


def top_gaps(results: list[dict], owners: dict[str, str], due: str | None) -> list[dict]:
    """The weakest criteria first (ties keep the criteria order), as tasks for the owners."""
    weakest = sorted(range(len(results)), key=lambda i: (results[i]["coverage"], i))[:MAX_GAPS]
    out = []
    for i in weakest:
        r = results[i]
        if r["coverage"] >= 100:
            continue
        out.append(
            {
                "criterion_id": r["id"], "criterion": r["name"], "coverage": r["coverage"],
                "task": f"Collect evidence for '{r['name']}'. Missing: {r['missing']}",
                "suggested_owner": owners.get(r["id"].upper()), "due": due,
            }
        )
    return out


def countdown(deadline: date, today: date, is_demo: bool) -> dict:
    days_left = (deadline - today).days
    tier = "overdue" if days_left < 0 else next((str(d) for d in sorted(REMINDER_DAYS) if days_left <= d), None)
    checkpoints = []
    for d in REMINDER_DAYS:
        when = deadline - timedelta(days=d)
        state = "passed" if today > when else "due_now" if today == when else "upcoming"
        checkpoints.append({"days_before": d, "date": when.isoformat(), "state": state})
    return {"deadline": deadline.isoformat(), "deadline_is_demo": is_demo, "days_left": days_left, "tier": tier, "checkpoints": checkpoints}


def parse_deadline(value) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None
