from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.chat import ChatMessage, MessageText

RankBy = Literal["relevance", "impact", "time_to_adopt"]


class Score(BaseModel):
    score: int = Field(ge=1, le=5)
    reason: str


class Scores(BaseModel):
    impact: Score = Field(description="1 = minor ... 5 = transformative")
    time_to_adopt: Score = Field(description="1 = within a year ... 5 = more than 5 years or unproven")
    relevance: Score = Field(description="Fit with Dubai Police priorities, 1 = low ... 5 = very high")


class HorizonSource(BaseModel):
    ref: int = Field(description="The [n] number used in the summary")
    title: str
    source_name: str
    source_url: str
    published_date: str | None = None


class HorizonCard(BaseModel):
    id: str
    topic: str
    topic_name: str
    trend: str
    theme: str
    summary: str
    scores: Scores
    so_what_for_dubai_police: str
    sources: list[HorizonSource]
    confidence: Literal["high", "medium", "low"]
    status: str = Field(default="draft", description="draft until the backend approval flow publishes it")
    generated_at: str


class HorizonTopicState(BaseModel):
    """Everything we remember about one topic (saved to the store file)."""

    cards: list[HorizonCard] = Field(default_factory=list)
    last_run: str | None = None
    signature: str | None = None  # which research items the cards were built from
    items_analyzed: int = 0
    last_error: str | None = None  # in memory only, never saved


class HorizonTopicResult(BaseModel):
    topic: str
    name: str
    count: int = Field(description="Cards returned (after top_n)")
    total_available: int = Field(description="Cards that exist for this topic")
    items_analyzed: int = Field(description="Research items the cards were built from")
    last_run: str | None = None
    cards: list[HorizonCard]
    warning: str | None = None


class HorizonResponse(BaseModel):
    topics: list[HorizonTopicResult]
    total: int
    rank_by: RankBy
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "Trend cards are AI-generated from the collected research items. Scores are an "
        "analyst-style estimate, not a measurement. Cards stay in draft until an officer "
        "approves them. Please check the original links."
    )
    warning: str | None = None


class HorizonPlan(BaseModel):
    """What the agent understood the person wants."""

    topics: list[str] = Field(description="Topic slugs")
    top_n: int | None = Field(default=None, description="Cards per topic, null = all")
    rank_by: RankBy = "relevance"
    reanalyze: bool = False


class HorizonChatRequest(BaseModel):
    message: MessageText = Field(description="The new message from the person.")
    history: list[ChatMessage] = Field(
        default_factory=list,
        max_length=20,
        description="Earlier messages of this conversation, oldest first. The caller stores and sends these.",
    )

    model_config = {
        "json_schema_extra": {
            "example": {"message": "Show me the trends", "history": []}
        }
    }


class HorizonChatResponse(BaseModel):
    type: Literal["question", "result", "message"] = Field(
        description="question = the agent needs more detail, result = cards are included, message = plain reply"
    )
    message: str
    suggestions: list[str] = Field(default_factory=list, description="Short replies to show as buttons")
    plan: HorizonPlan | None = None
    result: HorizonResponse | None = None
    model: str
