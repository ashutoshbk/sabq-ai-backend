"""Checks on the model's evaluation, and the priority score and recommendation (pure code)."""

from __future__ import annotations

from app.schemas.portfolio import CRITERIA
from app.services.agent_common import whole_between
from app.services.horizon_parsing import load_json_object
from app.services.trends_parsing import shorten

INVERTED = {"effort", "risk"}  # a higher score is worse
TO_BE_MEASURED = "to be measured"


def parse_evaluation(text: str | None, portfolio_codes: dict[str, str], objective_ids: set[str]) -> dict | None:
    """The checked evaluation, or None if unreadable. Every one of the five scores is required."""
    data = load_json_object(text)
    if not data or not isinstance(data.get("scores"), dict) or not isinstance(data.get("charter"), dict):
        return None

    scores = {}
    for crit in CRITERIA:
        raw = data["scores"].get(crit)
        value = whole_between(raw.get("score") if isinstance(raw, dict) else raw, 1, 5)
        if value is None:
            return None  # a missing score is never assumed
        scores[crit] = {"score": value, "reason": shorten(raw.get("reason") if isinstance(raw, dict) else "", 250) or "No reason given."}

    overlaps, seen = [], set()
    for raw in data.get("overlaps") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("code") or "").strip().upper()
        if code in portfolio_codes and code not in seen:  # only initiatives that really exist
            seen.add(code)
            overlaps.append({"code": code, "title": portfolio_codes[code], "reason": shorten(raw.get("reason"), 250) or "Covers a similar scope."})

    objectives = []
    for item in data.get("objective_ids") or []:
        oid = str(item).strip().upper()
        if oid in objective_ids and oid not in objectives:
            objectives.append(oid)

    c = data["charter"]
    kpis = []
    for raw in c.get("kpis") or []:
        if isinstance(raw, dict) and shorten(raw.get("name"), 120):
            kpis.append(
                {
                    "name": shorten(raw.get("name"), 120),
                    "baseline": shorten(raw.get("baseline"), 80) or TO_BE_MEASURED,
                    "target": shorten(raw.get("target"), 120) or "to be set",
                }
            )
    milestones = []
    for raw in c.get("milestones") or []:
        month = whole_between(raw.get("month") if isinstance(raw, dict) else None, 1, 36)
        if isinstance(raw, dict) and shorten(raw.get("title"), 160) and month:
            milestones.append({"title": shorten(raw.get("title"), 160), "month": month})
    milestones.sort(key=lambda m: m["month"])

    charter = {
        "title": shorten(c.get("title"), 160),
        "problem": shorten(c.get("problem"), 400),
        "objective": shorten(c.get("objective"), 300),
        "scope": shorten(c.get("scope"), 400),
        "kpis": kpis,
        "milestones": milestones,
        "owner_role": shorten(c.get("owner_role"), 80) or "To be assigned",
        "dependencies": [d for d in (shorten(x, 160) for x in c.get("dependencies") or []) if d],
    }
    if not (charter["title"] and charter["problem"] and charter["objective"]):
        return None
    return {"scores": scores, "overlaps": overlaps, "objectives": objectives, "charter": charter}


def priority_score(scores: dict[str, dict], weights: dict[str, float]) -> float:
    """Weighted average on a 1 to 5 scale. Effort and risk count in reverse, so a low effort adds to the score."""
    total = sum(weights.get(c, 0) for c in CRITERIA)
    if total <= 0:
        return 0.0
    value = sum(weights.get(c, 0) * ((6 - scores[c]["score"]) if c in INVERTED else scores[c]["score"]) for c in CRITERIA)
    return round(value / total, 2)


def recommend(priority: float, overlaps: list[dict], portfolio_status: dict[str, str], thresholds: dict[str, float]) -> tuple[str, str | None]:
    """Adopt, Defer, Merge or Reject. A real overlap with a live initiative wins: merge instead of starting a duplicate."""
    for o in overlaps:
        if portfolio_status.get(o["code"], "") != "rejected":
            return "Merge", o["code"]
    if priority >= thresholds["adopt"]:
        return "Adopt", None
    if priority >= thresholds["defer"]:
        return "Defer", None
    return "Reject", None


def rationale(priority: float, recommendation: str, merge_with: str | None, scores: dict[str, dict], thresholds: dict[str, float]) -> str:
    def effective(c):  # 5 is always best
        return (6 - scores[c]["score"]) if c in INVERTED else scores[c]["score"]

    best = max(CRITERIA, key=lambda c: (effective(c), -CRITERIA.index(c)))
    worst = min(CRITERIA, key=lambda c: (effective(c), CRITERIA.index(c)))
    parts = [f"Priority score {priority} of 5 (adopt from {thresholds['adopt']}, defer from {thresholds['defer']})."]
    if merge_with:
        parts.append(f"It overlaps with {merge_with}, so the recommendation is to merge it there instead of starting a duplicate.")
    parts.append(f"Strongest: {best} ({scores[best]['score']}). Weakest: {worst} ({scores[worst]['score']}).")
    if INVERTED & {best, worst}:
        parts.append("For effort and risk a lower score is better.")
    parts.append(f"Recommendation: {recommendation}. The committee decides.")
    return " ".join(parts)
