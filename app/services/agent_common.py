"""Small helpers shared by agents 4 to 8: demo data loading, saving, asking the model for JSON, checking quotes."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from openai import AsyncOpenAI

from app.core.config import Settings
from app.core.errors import AppError
from app.core.resources import RESOURCES_DIR
from app.core.store import JsonFileStore, resolve_store_path

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # a second try only if the first answer could not be read


# ---- demo data (app/resources/*.json) ----------------------------------------------------

def load_resource(name: str) -> dict:
    return json.loads((RESOURCES_DIR / name).read_text(encoding="utf-8"))


def load_platform() -> dict:
    return load_resource("platform_demo.json")


# ---- saving -----------------------------------------------------------------------------------

def make_store(settings: Settings, filename: str) -> JsonFileStore | None:
    folder = (settings.agents_store_dir or "").strip()
    if not folder:
        return None
    path = resolve_store_path(f"{folder}/{filename}")
    return JsonFileStore(path) if path else None


class Saved:
    """A dict of saved results, kept in a small JSON file. A damaged file is ignored."""

    def __init__(self, store: JsonFileStore | None):
        self._store = store
        self.items: dict[str, dict] = {}
        if store:
            data = store.load().get("items")
            if isinstance(data, dict):
                self.items = {k: v for k, v in data.items() if isinstance(v, dict)}

    def put(self, key: str, value: dict) -> None:
        self.items[key] = value
        if self._store:
            self._store.save({"version": 1, "items": self.items})


# ---- asking the model -------------------------------------------------------------------------

async def ask_json(
    client: AsyncOpenAI,
    settings: Settings,
    prompt: str,
    parse: Callable[[str | None], Any | None],
) -> Any:
    """One model call (no web search). `parse` returns None for an unreadable answer: then one retry."""
    kwargs = {
        "model": settings.agents_model,
        "input": prompt,
        "max_output_tokens": settings.agents_max_output_tokens,
        "store": False,
    }
    if settings.reasoning:
        kwargs["reasoning"] = settings.reasoning
    for attempt in range(1, MAX_ATTEMPTS + 1):
        resp = await client.responses.create(**kwargs)
        parsed = parse(resp.output_text)
        if parsed is not None:
            return parsed
        logger.warning("Agent answer unreadable (attempt %d of %d)", attempt, MAX_ATTEMPTS)
    raise AppError("no_valid_results", "The model returned an answer that could not be read. Please try again.", 502)


# ---- checking what the model says -------------------------------------------------------------

def normalize(text: Any) -> str:
    return " ".join(str(text or "").lower().replace("’", "'").split())


def quote_in(source: str, quote: Any) -> bool:
    """True if `quote` really appears in `source` (ignoring case and spacing). A short or empty quote never counts."""
    q = normalize(quote).strip(" .\"'“”")
    return len(q) >= 8 and q in normalize(source)


def short_id(text: str) -> str:
    return hashlib.sha1(" ".join(text.lower().split()).encode("utf-8")).hexdigest()[:8]


def whole_between(value: Any, low: int, high: int) -> int | None:
    try:
        number = round(float(value))
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None
