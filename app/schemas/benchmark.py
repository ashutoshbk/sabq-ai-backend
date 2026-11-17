from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---- configuration (app/resources/*.json) --------------------------------

class BenchmarkEntity(BaseModel):
    """One row of the platform's competitive table (demo data)."""

    id: str
    name: str
    org_kind: str = Field(description="police | ministry | government")
    country: str
    scope: str
    type: str
    is_self: bool = False
    demo_score: float | None = None
    demo_rank: str | None = None


class BenchmarkIndicator(BaseModel):
    """A published indicator to look for. Holds NO links: the agent finds the pages itself."""

    id: str
    name: str
    publisher: str
    level: Literal["country", "organization"]
    hint: str
    domains: list[str] = Field(max_length=20)
    applies_to_kinds: list[str] = Field(default_factory=list)
    comparable_across_entities: bool = True


# ---- what we remember ----------------------------------------------------

class StoredFinding(BaseModel):
    indicator_id: str
    subject: str = Field(description="A country name (country-level indicator) or an entity id")
    status: Literal["found", "not_found"]
    metric: str | None = None
    value: float | None = None
    scale_min: float | None = None
    scale_max: float | None = None
    higher_is_better: bool = True
    rank: int | None = None
    rank_of: int | None = None
    year: int | None = None
    normalized: float | None = Field(default=None, description="0 to 100, only for ranking inside one comparable group")
    basis: Literal["score", "rank"] | None = None
    source_name: str | None = None
    source_url: str | None = None
    evidence: str | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    checked_at: str


class BenchmarkState(BaseModel):
    findings: list[StoredFinding] = Field(default_factory=list)
    last_run: str | None = None


# ---- the API response ----------------------------------------------------

class Baseline(BaseModel):
    score: float | None = None
    rank: str | None = None
    label: str = "Platform demo data (not a published indicator)"


class EntityFinding(BaseModel):
    indicator_id: str
    indicator_name: str
    level: str
    status: Literal["found", "not_found"]
    describes: str = Field(description="What the figure is about, for example 'Singapore (the country as a whole)'")
    metric: str | None = None
    value: float | None = None
    scale_min: float | None = None
    scale_max: float | None = None
    rank: int | None = None
    rank_of: int | None = None
    year: int | None = None
    normalized: float | None = None
    source_name: str | None = None
    source_url: str | None = None
    evidence: str | None = None
    confidence: str | None = None
    comparable: bool = Field(description="True only if it was ranked together with others of the same indicator and year")
    group_rank: int | None = Field(default=None, description="Rank inside the comparable group")
    note: str


class EntityResult(BaseModel):
    id: str
    name: str
    org_kind: str
    country: str
    scope: str
    type: str
    is_self: bool
    baseline: Baseline
    findings: list[EntityFinding]


class RankedSubject(BaseModel):
    subject: str
    value: float | None = None
    normalized: float | None = None
    rank: int | None = None
    rank_of: int | None = None
    group_rank: int | None = None


class IndicatorResult(BaseModel):
    id: str
    name: str
    publisher: str
    level: str
    year: int | None = Field(default=None, description="Year of the comparable group, if there is one")
    compared: bool = Field(description="True if at least two subjects could be fairly ranked together")
    ranking: list[RankedSubject] = Field(default_factory=list)
    note: str


class DataCheck(BaseModel):
    code: str
    severity: Literal["warning", "info"]
    message: str
    entities: list[str] = Field(default_factory=list)


class BenchmarkResponse(BaseModel):
    entities: list[EntityResult]
    indicators: list[IndicatorResult]
    data_checks: list[DataCheck]
    insight: str | None = Field(
        default=None,
        description="A short summary built in plain code from the computed ranking. No model writes it.",
    )
    last_run: str | None = None
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "Figures were read by AI from published pages found with web search. Always check the "
        "source link. Figures about a country describe the country as a whole, not the "
        "organization. Entities are only ranked together when the same indicator and year are "
        "available for all of them. Baseline scores are platform demo data. Status is draft."
    )
    warning: str | None = None
