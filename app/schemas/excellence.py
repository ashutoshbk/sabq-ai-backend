from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Checkpoint(BaseModel):
    days_before: int
    date: str
    state: Literal["passed", "due_now", "upcoming"]


class Countdown(BaseModel):
    deadline: str
    deadline_is_demo: bool
    days_left: int
    tier: Literal["overdue", "30", "60", "90"] | None = Field(
        default=None, description="Which reminder applies today: 90, 60 or 30 days before the deadline"
    )
    checkpoints: list[Checkpoint]


class AwardInfo(BaseModel):
    id: str
    title: str
    org: str
    kind: str
    priority: str
    duration: str
    status: str
    criteria_available: bool = Field(description="False if no criteria are loaded yet (the agent then cannot assess it)")
    countdown: Countdown | None = None
    last_assessed: str | None = None
    readiness: float | None = None


class EvidenceMatch(BaseModel):
    evidence_id: str
    evidence_name: str
    why: str


class CriterionResult(BaseModel):
    id: str
    name: str
    matches: list[EvidenceMatch]
    coverage: int = Field(ge=0, le=100, description="0 to 100. Forced to 0 when no real evidence matches")
    missing: str
    no_evidence: bool


class GapTask(BaseModel):
    criterion_id: str
    criterion: str
    coverage: int
    task: str
    suggested_owner: str | None = None
    due: str | None = None


class Assessment(BaseModel):
    award: AwardInfo
    criteria_set: str
    readiness: float = Field(description="Average coverage over all criteria, computed in code")
    criteria: list[CriterionResult]
    top_gaps: list[GapTask]
    countdown: Countdown | None = None
    timeline_note: str
    last_run: str
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "Coverage was estimated by AI from short descriptions of the evidence library, not from the "
        "documents themselves. Only evidence that exists in the library index is counted. The "
        "criteria and the evidence library are demo data. Owners are suggestions. "
        "The readiness report needs approval before tasks are created. Status is draft."
    )


class AssessRequest(BaseModel):
    award_id: str = Field(description="An id from GET /agents/excellence/awards, for example efqm")
    deadline: str | None = Field(default=None, description="Submission deadline as YYYY-MM-DD (overrides the demo one)")
    refresh: bool = False

    model_config = {"json_schema_extra": {"example": {"award_id": "efqm", "refresh": False}}}
