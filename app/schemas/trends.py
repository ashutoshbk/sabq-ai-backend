from __future__ import annotations

from pydantic import BaseModel


class TrendCard(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    timeframe: str
    source_name: str
    source_url: str


class TrendsResponse(BaseModel):
    trends: list[TrendCard]
    count: int
    generated_at: str
    cached: bool = False
    model: str
    ai_generated: bool = True
    disclaimer: str = (
        "Summaries are AI-generated from web sources. Source names and links come "
        "from the search results. Please verify details at the original link."
    )
    warning: str | None = None
