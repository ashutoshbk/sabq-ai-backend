from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class UploadedFile(BaseModel):
    name: str = Field(max_length=200, description="File name, for example pilot-report.pdf")
    description: str | None = Field(default=None, max_length=400, description="What the file contains, in a sentence")


class MilestoneInfo(BaseModel):
    id: str
    title: str
    due: str


class InitiativeInfo(BaseModel):
    code: str
    title: str
    department: str
    milestones: list[MilestoneInfo]
    platform_status: str = Field(description="Execution status shown on the platform (demo data)")
    platform_progress: int = Field(description="Progress shown on the platform (demo data)")
    last_checked: str | None = None
    rag: str | None = None
    progress: int | None = None


class MilestoneCheck(BaseModel):
    id: str
    title: str
    due: str
    verdict: Literal["Yes", "Partial", "No"]
    reason: str
    evidence_files: list[str] = Field(default_factory=list, description="Uploaded files that really exist and support the verdict")
    evidence_quote: str | None = None
    days_late: int = Field(default=0, description="Days past the due date while not complete, computed in code")


class Reminder(BaseModel):
    to: str
    message: str
    escalate: bool = Field(description="True if the delay is long enough to tell the manager (computed in code)")
    missing: list[str]


class StatusCheck(BaseModel):
    initiative: InitiativeInfo
    milestones: list[MilestoneCheck]
    progress: int = Field(description="Percent: Yes counts 1, Partial counts 0.5. Computed in code")
    rag: Literal["Green", "Amber", "Red", "Complete"]
    rag_reason: str
    slip_risk: Literal["low", "medium", "high"]
    slip_reasons: list[str]
    risks: list[str]
    reminder: Reminder | None = None
    platform_progress: int
    progress_difference_note: str | None = None
    last_run: str
    generated_at: str
    model: str
    status: str = "draft"
    ai_generated: bool = True
    disclaimer: str = (
        "The model only judged whether each uploaded file or update supports each milestone. A "
        "milestone counts as done only if it cites an uploaded file that exists. Progress, status, "
        "delay risk and the reminder are computed in code from those judgements and the due dates. "
        "The initiatives and milestones are demo data. Status is draft."
    )


class CheckRequest(BaseModel):
    initiative_code: str = Field(description="A code from GET /agents/followup/initiatives, for example PRT-001")
    update_text: str = Field(default="", max_length=4000, description="The owner's latest update")
    files: list[UploadedFile] = Field(default_factory=list, max_length=30, description="Files the owner uploaded")

    model_config = {
        "json_schema_extra": {
            "example": {
                "initiative_code": "PRT-001",
                "update_text": "The requirements were approved in January. The pilot started in April and runs in two units.",
                "files": [{"name": "requirements-approval.pdf", "description": "Signed approval of the requirements"}],
            }
        }
    }


class DigestRow(BaseModel):
    code: str
    title: str
    department: str
    rag: str
    progress: int | None
    note: str


class Digest(BaseModel):
    generated_at: str
    counts: dict[str, int]
    rows: list[DigestRow]
    message: str = Field(description="A short message for the manager, built in code from the rows")
    status: str = "draft"
