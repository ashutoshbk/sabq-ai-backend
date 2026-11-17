"""Pure helper functions for the Horizon Scanning Agent: JSON reading, checking, ranking.

Kept apart from the service so they can be tested without any OpenAI calls.
"""

from __future__ import annotations

import calendar
import json
import re
from typing import Any

from app.services.trends_parsing import CATEGORIES, coerce_choice, shorten

RANK_OPTIONS = ["relevance", "impact", "time_to_adopt"]
SCORE_NAMES = ("impact", "time_to_adopt", "relevance")
CONFIDENCE_LEVELS = ["high", "medium", "low"]

MAX_TREND_CHARS = 120
MAX_SUMMARY_CHARS = 600
MAX_REASON_CHARS = 220
MAX_SO_WHAT_CHARS = 450
MAX_SUGGESTIONS = 4
MAX_SUGGESTION_CHARS = 60

_CITATION = re.compile(r"\[(\d+)\]")


def latest_possible_date(value: str | None) -> str | None:
    """The LAST day a partial date could mean, as YYYY-MM-DD: '2025' -> 2025-12-31, '2025-06' -> 2025-06-30.

    Used for the age limit, so a source dated only '2025' gets the benefit of the doubt
    instead of being treated as 1 January."""
    if not value:
        return None
    parts = value.split("-")
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 12
        day = int(parts[2]) if len(parts) > 2 else calendar.monthrange(year, month)[1]
    except (ValueError, IndexError):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def load_json_object(text: str | None) -> dict | None:
    """The first JSON object in the model's text. Tolerates code fences and chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
    try:
        data: Any = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _score(raw: Any) -> dict | None:
    """{'score': 1..5, 'reason': text}. A missing or non-numeric score is NOT guessed."""
    if not isinstance(raw, dict):
        return None
    value = raw.get("score")
    if isinstance(value, bool):
        return None
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return None
    reason = shorten(raw.get("reason"), MAX_REASON_CHARS)
    if not reason:
        return None
    return {"score": max(1, min(5, number)), "reason": reason}


def _refs(raw: Any, source_count: int) -> list[int]:
    """Source numbers that really exist, without repeats, in the order given."""
    found: list[int] = []
    for value in raw if isinstance(raw, list) else []:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= source_count and number not in found:
            found.append(number)
    return found


def parse_analysis(text: str | None, source_count: int, max_cards: int) -> list[dict]:
    """Turn the model's JSON into trend dicts, dropping anything we cannot stand behind.

    A trend is dropped when it has no real source, a missing score, or no summary.
    Citations like [9] that point at a source that does not exist are removed from the text.
    """
    data = load_json_object(text)
    trends = (data or {}).get("trends")
    if not isinstance(trends, list):
        return []

    cards: list[dict] = []
    for raw in trends:
        if not isinstance(raw, dict):
            continue
        refs = _refs(raw.get("source_refs"), source_count)
        scores_raw = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
        scores = {name: _score(scores_raw.get(name)) for name in SCORE_NAMES}
        trend = shorten(raw.get("trend"), MAX_TREND_CHARS)
        summary = shorten(raw.get("summary"), MAX_SUMMARY_CHARS)
        so_what = shorten(raw.get("so_what_for_dubai_police"), MAX_SO_WHAT_CHARS)

        if not (trend and summary and so_what and refs) or any(v is None for v in scores.values()):
            continue

        summary = _CITATION.sub(
            lambda m: m.group(0) if 1 <= int(m.group(1)) <= source_count else "", summary
        )
        summary = " ".join(summary.split())
        cards.append(
            {
                "trend": trend,
                "theme": coerce_choice(raw.get("theme"), CATEGORIES, "General"),
                "summary": summary,
                "scores": scores,
                "so_what_for_dubai_police": so_what,
                "source_refs": refs,
                "confidence": coerce_choice(raw.get("confidence"), CONFIDENCE_LEVELS, "low"),
            }
        )
        if len(cards) >= max_cards:
            break
    return cards


def parse_plan(text: str | None) -> dict | None:
    """The planner's answer: {'action', 'message', 'suggestions', 'plan'}, or None if unreadable."""
    data = load_json_object(text)
    if not data:
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in ("ask", "run", "message"):
        return None
    suggestions = []
    for item in data.get("suggestions") or []:
        short = shorten(item, MAX_SUGGESTION_CHARS)
        if short and short not in suggestions:
            suggestions.append(short)
    plan = data.get("plan")
    return {
        "action": action,
        "message": shorten(data.get("message"), 500),
        "suggestions": suggestions[:MAX_SUGGESTIONS],
        "plan": plan if isinstance(plan, dict) else None,
    }


def clean_plan(raw: dict | None, valid_slugs: list[str], max_cards: int) -> dict:
    """Make a safe plan from whatever the planner wrote. Unknown values fall back to defaults."""
    raw = raw or {}

    wanted = raw.get("topics")
    if isinstance(wanted, str):
        wanted = [wanted]
    slugs: list[str] = []
    for value in wanted if isinstance(wanted, list) else []:
        slug = str(value or "").strip().lower()
        if slug in valid_slugs and slug not in slugs:
            slugs.append(slug)
    if not slugs:  # "all", nothing, or only unknown names
        slugs = list(valid_slugs)

    top_n = raw.get("top_n")
    if isinstance(top_n, bool) or not isinstance(top_n, (int, float)) or top_n < 1:
        top_n = None
    else:
        top_n = min(int(top_n), max_cards)

    return {
        "topics": slugs,
        "top_n": top_n,
        "rank_by": coerce_choice(raw.get("rank_by"), RANK_OPTIONS, "relevance"),
        "reanalyze": raw.get("reanalyze") is True,
    }


def rank_cards(cards: list, rank_by: str) -> list:
    """Order cards. Ties keep the order the agent wrote them in."""

    def key(card) -> tuple:
        s = card.scores
        if rank_by == "impact":
            return (-s.impact.score, -s.relevance.score, s.time_to_adopt.score)
        if rank_by == "time_to_adopt":  # soonest first
            return (s.time_to_adopt.score, -s.relevance.score, -s.impact.score)
        return (-s.relevance.score, -s.impact.score, s.time_to_adopt.score)

    return sorted(cards, key=key)
