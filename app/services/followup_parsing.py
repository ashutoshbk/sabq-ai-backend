"""Checks on the model's milestone judgements, and progress, status, delay risk and reminder (pure code)."""

from __future__ import annotations

from datetime import date

from app.services.agent_common import normalize, quote_in
from app.services.horizon_parsing import load_json_object
from app.services.trends_parsing import shorten

VERDICTS = {"yes": "Yes", "partial": "Partial", "no": "No"}
ESCALATE_AFTER_DAYS = 30
SOON_DAYS = 30


def parse_check(text: str | None, milestones: list[dict], files: list[dict], update_text: str) -> dict | None:
    """Checked verdicts per milestone and risks, or None if unreadable.

    A verdict of Yes needs a cited uploaded file that really exists. Partial needs a real file or a real
    quote from the update. Anything weaker becomes No (or Partial for a Yes that only has a quote)."""
    data = load_json_object(text)
    if not data or not isinstance(data.get("milestones"), list):
        return None
    file_by_key = {normalize(f["name"]): f["name"] for f in files}
    raw_by_id = {}
    for raw in data["milestones"]:
        if isinstance(raw, dict):
            raw_by_id.setdefault(str(raw.get("id") or "").strip().upper(), raw)

    out = []
    for m in milestones:
        raw = raw_by_id.get(m["id"].upper()) or {}
        verdict = VERDICTS.get(str(raw.get("verdict") or "").strip().lower(), "No")
        reason = shorten(raw.get("reason"), 250)
        cited = []
        for name in raw.get("evidence_files") or []:
            real = file_by_key.get(normalize(name))
            if real and real not in cited:
                cited.append(real)
        quote = shorten(raw.get("evidence_quote"), 250) or None
        quote_ok = bool(quote) and quote_in(update_text, quote)
        if not quote_ok:
            quote = None

        if verdict == "Yes" and not cited:
            verdict, reason = ("Partial" if quote_ok else "No"), f"Not accepted as done: no uploaded file supports it. {reason}".strip()
        elif verdict == "Partial" and not cited and not quote_ok:
            verdict, reason = "No", f"No uploaded file or line of the update supports it. {reason}".strip()
        elif verdict == "No":
            cited, quote = [], None
        out.append(
            {"id": m["id"], "title": m["title"], "due": m["due"], "verdict": verdict,
             "reason": reason or "No reason given.", "evidence_files": cited, "evidence_quote": quote}
        )
    risks = [r for r in (shorten(x, 200) for x in data.get("risks") or []) if r]
    return {"milestones": out, "risks": risks[:6]}


def progress_of(milestones: list[dict]) -> int:
    if not milestones:
        return 0
    points = sum(1 if m["verdict"] == "Yes" else 0.5 if m["verdict"] == "Partial" else 0 for m in milestones)
    return round(100 * points / len(milestones))


def add_lateness(milestones: list[dict], today: date) -> None:
    for m in milestones:
        due = date.fromisoformat(m["due"])
        m["days_late"] = max(0, (today - due).days) if m["verdict"] != "Yes" else 0


def rag_status(milestones: list[dict], today: date) -> tuple[str, str]:
    if milestones and all(m["verdict"] == "Yes" for m in milestones):
        return "Complete", "Every milestone has supporting evidence."
    late = [m for m in milestones if m["days_late"] > 0]
    if not late:
        return "Green", "No milestone is past its due date without evidence."
    worst = max(late, key=lambda m: m["days_late"])
    if worst["days_late"] > ESCALATE_AFTER_DAYS:
        return "Red", f"{len(late)} milestone(s) are past due without full evidence. The longest delay is {worst['days_late']} days ({worst['id']} {worst['title']})."
    return "Amber", f"{len(late)} milestone(s) are past due without full evidence, by at most {worst['days_late']} days."


def slip_risk(milestones: list[dict], today: date) -> tuple[str, list[str]]:
    """Low, medium or high, from simple rules the reader can check."""
    reasons = []
    late = [m for m in milestones if m["days_late"] > 0]
    if late:
        reasons.append(f"{len(late)} milestone(s) are already late.")
    soon = [
        m for m in milestones
        if m["verdict"] == "No" and m["days_late"] == 0 and 0 <= (date.fromisoformat(m["due"]) - today).days <= SOON_DAYS
    ]
    if soon:
        reasons.append(f"{len(soon)} milestone(s) are due within {SOON_DAYS} days with no evidence yet.")
    if any(m["days_late"] > ESCALATE_AFTER_DAYS for m in milestones):
        return "high", reasons
    if reasons:
        return "medium", reasons
    return "low", ["No late milestone and nothing due soon without evidence."]


def build_reminder(initiative: dict, milestones: list[dict]) -> dict | None:
    """A polite English reminder that asks only for what is missing. None if everything is done."""
    pending = [m for m in milestones if m["verdict"] != "Yes"]
    if not pending:
        return None
    lines, missing = [], []
    for m in pending:
        ask = "supporting evidence" if m["verdict"] == "Partial" else "the evidence file or a short update"
        lines.append(f"- {m['id']} {m['title']} (due {m['due']}): please send {ask}.")
        missing.append(f"{m['id']} {m['title']}")
    message = (
        f"Dear {initiative['department']} team,\n"
        f"Thank you for your update on '{initiative['title']}' ({initiative['code']}). "
        "To record progress, we still need the following:\n" + "\n".join(lines) + "\nThank you."
    )
    escalate = any(m["days_late"] > ESCALATE_AFTER_DAYS for m in pending)
    return {"to": initiative["department"], "message": message, "escalate": escalate, "missing": missing}
