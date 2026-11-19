from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

CRITERIA = ["alignment", "impact", "effort", "risk", "urgency"]


class PortfolioItem(BaseModel):
    code: str
    type: str
    priority: str
    title: str
    department: str
    due: str | None = None
    status: str
    execution: str = ""
    progress: int = 0


class Opportunity(BaseModel):
    id: str = Field(description="practice:<gap>:<id>, trend:<topic>, or custom-xxxxxxxx")
    source: Literal["Best Practices", "Horizon Scan", "Custom"]
    title: str
    description: str
    evaluated: bool = False


class CriterionScore(BaseModel):
    criterion: Literal["alignment", "impact", "effort", "risk", "urgency"]
    score: int = Field(ge=1, le=5, description="1 to 5. For effort and risk a HIGHER score means MORE effort or risk")
    reason: str
    weight: float


class Overlap(BaseModel):
    code: str
    title: str
    reason: str


class Kpi(BaseModel):
    name: str
    baseline: str
    target: str


class Milestone(BaseModel):
    title: str
    month: int = Field(ge=1, le=36, description="Months after approval")


class Charter(BaseModel):
    title: str
    problem: str
    objective: str
    scope: str
    kpis: list[Kpi]
    milestones: list[Milestone]
    owner_role: str
    dependencies: list[str]


class Evaluation(BaseModel):
    opportunity: Opportunity
    scores: list[CriterionScore]
    priority_score: float = Field(description="1 to 5. Weighted average computed in code (effort and risk count in reverse)")
    overlaps: list[Overlap]
    objectives: list[str] = Field(description="Strategic objectives this serves (from the demo list)")
    recommendation: Literal["Adopt", "Defer", "Merge", "Reject"]
    merge_with: str | None = None
    rationale: str = Field(description="Built in code from the scores and the rule, so it matches the numbers")
    charter: Charter
    decision: str = Field(default="pending", description="pending until the committee decides")
    last_run: str
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "Scores and the charter were written by AI from the opportunity text. The priority score and "
        "the recommendation are computed in code from the scores, using demo weights and thresholds. "
        "The portfolio and objectives are demo data. Charter baselines are marked 'to be measured' "
        "unless known. The committee decides. Status is draft."
    )


class EvaluateRequest(BaseModel):
    opportunity_id: str | None = Field(default=None, description="An id from GET /agents/portfolio/opportunities")
    title: str | None = Field(default=None, max_length=160, description="Or describe a new opportunity: its title")
    description: str | None = Field(default=None, max_length=2000, description="and a few sentences")
    refresh: bool = False

    model_config = {"json_schema_extra": {"example": {"title": "AI call triage", "description": "Use AI to transcribe and triage non-emergency calls to free officer time."}}}


class OpportunitiesResponse(BaseModel):
    opportunities: list[Opportunity]
    note: str = "Opportunities come from the saved Best Practices and Horizon Scan cards, or you can send your own."
