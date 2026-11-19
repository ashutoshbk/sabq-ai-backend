from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ISSUE_TYPES = ["duplicate_approval", "unnecessary_handoff", "rework_loop", "missing_kpi", "other"]


class ProcessInfo(BaseModel):
    code: str
    name: str
    demo_maturity: int | None = Field(default=None, description="The maturity shown on the platform tile (demo data)")
    sop_available: bool = Field(description="True if SOP text is available for this process")
    analyzed_at: str | None = None


class Step(BaseModel):
    id: str
    action: str
    owner: str | None = None
    system: str | None = None
    est_time: str | None = None


class Issue(BaseModel):
    type: Literal["duplicate_approval", "unnecessary_handoff", "rework_loop", "missing_kpi", "other"]
    description: str
    step_ids: list[str] = Field(default_factory=list)
    evidence: str | None = None
    evidence_verified: bool = Field(description="True if the quoted evidence really appears in the SOP text")


class MaturityDimension(BaseModel):
    id: str
    name: str
    level: int = Field(ge=1, le=5)
    reason: str
    evidence: str | None = None
    evidence_verified: bool


class Maturity(BaseModel):
    overall: float | None = Field(default=None, description="Average of the dimensions, computed in code")
    dimensions: list[MaturityDimension]


class FlowStep(BaseModel):
    id: str
    action: str
    replaces: list[str] = Field(default_factory=list, description="Ids of current steps this step replaces")


class ProcessRef(BaseModel):
    code: str
    name: str
    origin: str


class ProcessAnalysis(BaseModel):
    process: ProcessRef
    steps: list[Step]
    issues: list[Issue]
    maturity: Maturity
    steps_before: int
    steps_after: int
    proposed_flow: list[FlowStep]
    needs_clarification: list[str]
    expected_time_saving: str
    platform_maturity: int | None = Field(default=None, description="The demo tile value, shown for comparison only")
    last_run: str
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "Steps, issues and scores were read by AI from the SOP text. A score counts only when its "
        "evidence line really appears in the SOP. Policy and legal requirements are not changed. "
        "The proposed flow and time saving are estimates. The process owner must approve before "
        "any platform tile is updated. Status is draft."
    )


class AnalyzeRequest(BaseModel):
    process_code: str | None = Field(default=None, description="A code from GET /agents/process/processes, for example OPS-001")
    sop_text: str | None = Field(default=None, max_length=12000, description="Or paste the SOP text")
    title: str | None = Field(default=None, max_length=120, description="A name for pasted text")
    refresh: bool = Field(default=False, description="Analyse again even if a result is saved")

    model_config = {"json_schema_extra": {"example": {"process_code": "SAMPLE-LEAVE", "refresh": False}}}
