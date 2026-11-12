"""Loads prompts and static resource files from disk."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent.parent
PROMPTS_DIR = APP_DIR / "prompts"
RESOURCES_DIR = APP_DIR / "resources"


class DomainEntry(BaseModel):
    domain: str
    name: str
    level: int


class Topic(BaseModel):
    slug: str
    name: str
    focus: str


@lru_cache
def load_topics() -> list[Topic]:
    data = json.loads((RESOURCES_DIR / "topics.json").read_text(encoding="utf-8"))
    return [Topic(**item) for item in data]


@lru_cache
def load_prompt(filename: str) -> str:
    return (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()


@lru_cache
def load_allowed_domains() -> list[DomainEntry]:
    data = json.loads((RESOURCES_DIR / "allowed_domains.json").read_text(encoding="utf-8"))
    return [DomainEntry(**item) for item in data]


@lru_cache
def load_quick_questions() -> list[str]:
    return json.loads((RESOURCES_DIR / "quick_questions.json").read_text(encoding="utf-8"))
