from __future__ import annotations

from pydantic import BaseModel, Field


class StoredItem(BaseModel):
    """One research item, as kept in memory and in the store file."""

    title: str
    summary: str
    category: str
    published_date: str | None = None  # YYYY-MM-DD, YYYY-MM or YYYY, only if found on the page
    source_name: str
    source_url: str
    first_seen: str  # when we first found it (UTC, ISO format)


class TrendItem(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    published_date: str | None = None
    source_name: str
    source_url: str
    first_seen: str


class TopicInfo(BaseModel):
    slug: str
    name: str


class TopicTrends(BaseModel):
    topic: str = Field(description="Topic slug, for example digital-twin")
    name: str
    count: int
    items: list[TrendItem]
    last_checked: str | None = Field(default=None, description="Last time a search ran for this topic")
    last_changed: str | None = Field(default=None, description="Last time a search found something new")
    new_in_last_refresh: int = 0
    warning: str | None = None


class TrendsResponse(BaseModel):
    topics: list[TopicTrends]
    total: int
    generated_at: str
    cached: bool = False
    model: str
    ai_generated: bool = True
    disclaimer: str = (
        "Summaries are AI-generated from web sources. Source names and links come "
        "from the search results, and dates are shown only when found on the page. "
        "Please verify details at the original link."
    )
    warning: str | None = None


class TopicState(BaseModel):
    """Everything we remember about one topic."""

    items: list[StoredItem] = Field(default_factory=list)
    last_checked: str | None = None
    last_changed: str | None = None
    last_new_count: int = 0
    last_error: str | None = None  # in memory only, never saved
