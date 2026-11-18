from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.chat import ChatMessage, MessageText

Effort = Literal["S", "M", "L"]


# ---- the gap the agent works on -------------------------------------------

class Gap(BaseModel):
    """What we want practices for. Built in code from Agent 1's trend cards and the platform
    demo data, or typed by the person."""

    id: str = Field(description="A topic slug such as digital-twin, or custom-xxxxxxxx for a typed gap")
    title: str
    description: str = Field(description="The text given to the search, so the person can see what was asked")
    origin: Literal["trend", "custom"]
    topic: str | None = None
    topic_name: str | None = None
    based_on: list[str] = Field(default_factory=list, description="Titles of Agent 1 trend cards used")
    platform_context: str | None = Field(default=None, description="Platform demo data used, if any")
    strategic_gap_percent: float | None = None
    priority: int | None = Field(default=None, description="1 = most pressing, from the platform gap % and trend relevance")
    practices_count: int = 0
    last_run: str | None = None


# ---- one practice card -----------------------------------------------------

class Practice(BaseModel):
    id: str
    rank: int = Field(description="1 = recommended first. Computed in code, not by the model")
    organization: str
    country: str | None = None
    title: str
    what_they_did: str
    result: str = Field(description="The measurable result, as published")
    result_kind: Literal["achieved", "estimated"] = Field(
        description="achieved = an outcome already measured or reported; estimated = a forecast or 'if adopted' figure"
    )
    result_year: int | None = None
    evidence: str | None = Field(default=None, description="A short phrase from the page that shows the result")
    prerequisites: str
    effort: Effort | None = Field(default=None, description="S / M / L, the analyst's estimate, null if not stated")
    expected_benefit: str
    adaptation_for_dubai_police: str
    risks: str
    source_name: str = Field(description="From our own trusted-domain list, never written by the model")
    source_url: str
    confidence: Literal["high", "medium", "low"]
    status: str = Field(default="draft", description="draft until an officer approves")
    handoff: str = Field(
        default="not_sent",
        description="not_sent | ready. Approved practices go to the Improvement Portfolio (Agent 6) as candidate initiatives.",
    )
    generated_at: str


class StoredGap(BaseModel):
    """What we remember about one gap (saved to the store file)."""

    gap: Gap
    practices: list[Practice] = Field(default_factory=list)
    last_run: str | None = None
    basis: str | None = Field(default=None, description="Fingerprint of the gap text the practices were found for")


# ---- the API response -------------------------------------------------------

class BestPracticesResponse(BaseModel):
    gap: Gap
    practices: list[Practice]
    recommendation: str | None = Field(
        default=None, description="Built in plain code from the ranking, so it matches the cards"
    )
    outdated: bool = Field(
        default=False,
        description="True if Agent 1's trend cards or the platform data changed since these practices were found",
    )
    last_run: str | None = None
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "Practices were read by AI from published pages found with web search. Check each source "
        "link. 'Effort' and the adaptation notes are analyst-style estimates, not measurements. "
        "Platform data used for the fit is demo data. Cards stay in draft until an officer approves them."
    )
    warning: str | None = None


class GapsResponse(BaseModel):
    gaps: list[Gap]
    note: str = (
        "Gaps are built from Agent 1's trend cards and the platform demo data. You can also send your own text."
    )


class RunRequest(BaseModel):
    gap_id: str | None = Field(default=None, description="A gap id from GET /agents/best-practices/gaps")
    custom_gap: str | None = Field(default=None, max_length=600, description="Or describe the gap in your own words")
    refresh: bool = Field(default=False, description="Search again even if practices are already saved")

    model_config = {"json_schema_extra": {"example": {"gap_id": "digital-twin", "refresh": False}}}


# ---- the conversation --------------------------------------------------------

class PracticePlan(BaseModel):
    gap_id: str | None = None
    custom_gap: str | None = None
    refresh: bool = False


class PracticeChatRequest(BaseModel):
    message: MessageText = Field(description="The new message from the person.")
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=20,
        description="Earlier messages of this conversation, oldest first. The caller stores and sends these.",
    )

    model_config = {"json_schema_extra": {"example": {"message": "How do others use digital twins?", "history": []}}}


class PracticeChatResponse(BaseModel):
    type: Literal["question", "result", "message"]
    message: str
    suggestions: list[str] = Field(default_factory=list)
    plan: PracticePlan | None = None
    result: BestPracticesResponse | None = None
    model: str
