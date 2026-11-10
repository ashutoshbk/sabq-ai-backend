"""Pure helper functions for the news feed: URL cleanup, citation collection, JSON parsing.

Kept separate from the service so they are easy to test without any OpenAI calls.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.core.resources import DomainEntry

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "trk",  # LinkedIn-style tracking, seen on a weforum.org link
    "trkcampaign",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}

CATEGORIES = [
    "Strategic Monitoring",
    "Strategic Thinking",
    "Policies",
    "Performance Indicators",
    "Global Competitiveness",
]
TIMEFRAMES = ["Under 1 year", "1-2 years", "2-3 years", "3-5 years", "5+ years"]

MAX_SUMMARY_CHARS = 400
MAX_TITLE_CHARS = 160


def clean_url(url: str | None) -> str | None:
    """Return the URL without tracking params (OpenAI adds ?utm_source=openai) and fragment.

    Returns None if it is not a valid http(s) URL.
    """
    url = (url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path, urlencode(query), "")
    )


def url_key(url: str | None) -> str | None:
    """A comparison key: cleaned URL without a trailing slash."""
    cleaned = clean_url(url)
    if cleaned is None:
        return None
    return cleaned.rstrip("/")


def match_domain(url: str, domains: list[DomainEntry]) -> DomainEntry | None:
    """Return the allowed-domain entry the URL belongs to (subdomains count), else None."""
    host = (urlsplit(url).hostname or "").lower()
    for entry in domains:
        d = entry.domain.lower()
        if host == d or host.endswith("." + d):
            return entry
    return None


def collect_citation_urls(node: Any) -> set[str]:
    """Walk the response (as a dict) and collect every URL OpenAI says it cited or read.

    Looks at `url_citation` annotations and `web_search_call` sources.
    """
    found: set[str] = set()

    def walk(item: Any) -> None:
        if isinstance(item, dict):
            if item.get("type") in ("url_citation", "url") and isinstance(item.get("url"), str):
                key = url_key(item["url"])
                if key:
                    found.add(key)
            for value in item.values():
                walk(value)
        elif isinstance(item, list):
            for value in item:
                walk(value)

    walk(node)
    return found


def parse_cards_json(text: str) -> list[dict]:
    """Extract the list of trend dicts from the model's text. Tolerates code fences and chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)

    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                data = None

    if isinstance(data, dict):
        data = data.get("trends")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def coerce_choice(value: Any, allowed: list[str], default: str) -> str:
    """Case-insensitive match against an allowed list, else the default."""
    text = str(value or "").strip().lower()
    for option in allowed:
        if text == option.lower():
            return option
    return default


def shorten(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
