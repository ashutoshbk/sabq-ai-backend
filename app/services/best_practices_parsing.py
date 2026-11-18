"""Pure helpers for the Best Practices Curator: checking what the model found, ranking, wording.

The model only reads pages. Whether a practice is kept, how practices are ranked and the
recommendation text are all decided here in plain code.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from typing import Any

from app.core.resources import DomainEntry
from app.schemas.best_practices import Practice
from app.services.benchmark_parsing import _whole
from app.services.horizon_parsing import load_json_object
from app.services.trends_parsing import (
    clean_url,
    coerce_choice,
    collect_citation_urls,
    match_domain,
    shorten,
    url_key,
)

logger = logging.getLogger(__name__)

CONFIDENCE_LEVELS = ["high", "medium", "low"]
CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
KIND_ORDER = {"achieved": 0, "estimated": 1}
EFFORT_ORDER = {"S": 0, "M": 1, "L": 2, None: 3}
EFFORT_WORDS = {
    "s": "S", "small": "S", "low": "S",
    "m": "M", "medium": "M", "moderate": "M",
    "l": "L", "large": "L", "high": "L",
}
MIN_YEAR = 2000
_HAS_NUMBER = re.compile(
    r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million|billion|"
    r"half|double|doubled|triple|tripled|percent|per cent)\b",
    re.IGNORECASE,
)
MIN_CUSTOM_CHARS = 8
MAX_CUSTOM_CHARS = 400
MAX_SUGGESTIONS = 4
MAX_SUGGESTION_CHARS = 70

_FUNDING = re.compile(r"\b(?:invest\w*|fund\w*|budget\w*|allocat\w*|spend\w*|spent|grant\w*)\b", re.IGNORECASE)
_OUTCOME = re.compile(
    r"\b(?:sav\w+|reduc\w+|cut|cuts|increas\w+|improv\w+|faster|quicker|fewer|fell|rose|"
    r"arrest\w*|convict\w*|prosecut\w*|solved|detect\w+|clear\w*|identif\w+|free[sd]?|hours|days|minutes|"
    r"accuracy|rate|rates|%|percent|per cent)\b|%",
    re.IGNORECASE,
)


def is_funding_only(result: str) -> bool:
    """True for 'supported X with over 50m of investment': money put in is an input, not a result."""
    return bool(_FUNDING.search(result)) and not _OUTCOME.search(result)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.rstrip(",") for n in _NUMBER.findall(text or "")}


def drop_repeated_results(sorted_practices: list[dict]) -> list[dict]:
    """The same result from the same page, told twice under two titles, is one practice.

    Input must already be ranked best first. A later practice is dropped when it comes from the
    same page as an earlier one and shares at least two figures with it."""
    kept: list[dict] = []
    for item in sorted_practices:
        figures = _numbers(item["result"])
        key = url_key(item["source_url"])
        repeated = any(
            url_key(k["source_url"]) == key and len(figures & _numbers(k["result"])) >= 2 for k in kept
        )
        if repeated:
            logger.info("Dropped '%s': the same result from the same page is already shown", item["title"])
            continue
        kept.append(item)
    return kept


def effort_of(value: Any) -> str | None:
    return EFFORT_WORDS.get(str(value or "").strip().lower())


def custom_gap_id(text: str) -> str:
    """The same words always give the same id, so asking twice reuses the saved practices."""
    key = " ".join(text.lower().split())
    return "custom-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def clean_custom_gap(text: Any) -> str | None:
    """The typed gap as one clean line, or None if it is too short to search for."""
    cleaned = shorten(text, MAX_CUSTOM_CHARS)
    return cleaned if len(cleaned) >= MIN_CUSTOM_CHARS else None


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def parse_practices(
    text: str | None,
    dump: dict,
    domains: list[DomainEntry],
    today: date,
) -> list[dict] | None:
    """Checked practices (without id, rank or time), or None when the answer was not readable.

    A practice is kept only if: its page was cited by the search tool, the page is on one of our
    trusted domains, and it names an organization, what was done AND a measurable result.
    The source name comes from our own domain list.
    """
    data = load_json_object(text)
    raw_list = (data or {}).get("practices")
    if not isinstance(raw_list, list):
        return None

    cited = collect_citation_urls(dump)
    kept: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for raw in raw_list:
        if not isinstance(raw, dict):
            continue
        organization = shorten(raw.get("organization"), 100)
        title = shorten(raw.get("title"), 120)
        what = shorten(raw.get("what_they_did"), 500)
        result = shorten(raw.get("result"), 400)
        if not (organization and title and what and result):
            logger.info("Dropped a practice: organization, title, action or result missing")
            continue
        kind = str(raw.get("result_kind") or "").strip().lower()
        if kind not in KIND_ORDER:  # "none", funding only, or not stated: there is no result
            logger.info("Dropped '%s': no achieved or estimated result (kind=%r)", title, kind)
            continue
        if is_funding_only(result):
            logger.info("Dropped '%s': the result is only funding or investment", title)
            continue
        if not _HAS_NUMBER.search(result):
            logger.info("Dropped '%s': the result has no number", title)
            continue

        url = clean_url(raw.get("source_url"))
        if not url or url_key(url) not in cited:
            logger.info("Dropped '%s': its source was not cited by the search tool", title)
            continue
        domain = match_domain(url, domains)
        if not domain:
            logger.info("Dropped '%s': %s is not on the trusted domains", title, url)
            continue

        key = (organization.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)

        year = _whole(raw.get("result_year"))
        if year is not None and not (MIN_YEAR <= year <= today.year):
            year = None

        kept.append(
            {
                "organization": organization,
                "country": shorten(raw.get("country"), 60) or None,
                "title": title,
                "what_they_did": what,
                "result": result,
                "result_kind": kind,
                "result_year": year,
                "evidence": shorten(raw.get("evidence"), 250) or None,
                "prerequisites": shorten(raw.get("prerequisites"), 400) or "Not stated on the source page.",
                "effort": effort_of(raw.get("effort")),
                "expected_benefit": shorten(raw.get("expected_benefit"), 400) or result,
                "adaptation_for_dubai_police": shorten(raw.get("adaptation_for_dubai_police"), 500)
                or "Not assessed: information about our own process is needed.",
                "risks": shorten(raw.get("risks"), 400) or "No risk named on the source page.",
                "source_name": domain.name,
                "source_url": url,
                "confidence": coerce_choice(raw.get("confidence"), CONFIDENCE_LEVELS, "low"),
            }
        )
    return kept


def rank_key(practice: dict | Practice) -> tuple:
    """Best evidence first, achieved results before estimates, then smaller effort, then recent results."""
    get = practice.get if isinstance(practice, dict) else lambda k: getattr(practice, k)
    return (
        CONFIDENCE_ORDER.get(get("confidence"), 3),
        KIND_ORDER.get(get("result_kind"), 2),
        EFFORT_ORDER.get(get("effort"), 3),
        -(get("result_year") or 0),
    )


def build_recommendation(practices: list[Practice], gap_title: str) -> str | None:
    """A short recommendation built from the ranking. No model writes it, so it matches the cards."""
    if not practices:
        return None

    def line(p: Practice) -> str:
        effort = f"effort {p.effort}" if p.effort else "effort not stated"
        year = f", result from {p.result_year}" if p.result_year else ""
        kind = "estimated result" if p.result_kind == "estimated" else "achieved result"
        return f"{p.title} ({p.organization}): {p.confidence} evidence, {kind}, {effort}{year}"

    first = practices[0]
    text = (
        f"For '{gap_title}', start with {line(first)}. Practices are ranked by strength of evidence, "
        "then achieved results before estimates, then smaller effort, then recent results."
    )
    if len(practices) > 1:
        text += " Next: " + "; ".join(line(p) for p in practices[1:3]) + "."
    unrated = [p for p in practices if not p.effort]
    if unrated:
        text += f" {len(unrated)} practice(s) have no effort estimate."
    text += " These are options for an officer to review, not a decision."
    return text


# ---- the conversation planner ---------------------------------------------------

def parse_plan(text: str | None) -> dict | None:
    """The planner's answer: {'action', 'message', 'suggestions', 'plan'}, or None if unreadable."""
    data = load_json_object(text)
    if not data:
        return None
    action = str(data.get("action") or "").strip().lower()
    if action not in ("ask", "run", "message"):
        return None
    suggestions: list[str] = []
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


def clean_plan(raw: dict | None, valid_ids: list[str]) -> dict:
    """A safe plan. A known gap id wins. Otherwise a typed gap (if long enough). Otherwise neither."""
    raw = raw or {}
    gap_id = str(raw.get("gap_id") or "").strip().lower()
    if gap_id not in valid_ids:
        gap_id = ""
    custom = None if gap_id else clean_custom_gap(raw.get("custom_gap"))
    return {"gap_id": gap_id or None, "custom_gap": custom, "refresh": raw.get("refresh") is True}

