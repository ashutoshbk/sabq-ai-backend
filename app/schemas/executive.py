from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BriefLine(BaseModel):
    text: str
    source: str = Field(description="The module the line comes from, for example 'Agent 5 Excellence Radar'")


class QualityCheck(BaseModel):
    code: str
    severity: Literal["warning", "info"]
    module: str
    message: str


class ModuleStatus(BaseModel):
    module: str
    last_run: str | None
    items: int = Field(description="Saved results or cards in this module")
    state: Literal["fresh", "stale", "not run"]


class ExecutiveBrief(BaseModel):
    what_changed: list[BriefLine]
    risks: list[BriefLine]
    decisions_needed: list[BriefLine] = Field(description="At most 3")
    next_30_days: list[BriefLine]
    word_count: int
    generated_at: str
    status: str = "draft"
    ai_generated: bool = False
    disclaimer: str = (
        "Built in plain code from the saved results of Agents 1 to 7 and the demo platform data. "
        "No model writes it, and every line names its source. Results are drafts: check them "
        "before they are shared. The weekly (Sunday) schedule is left to the backend developer."
    )


class DataQualityReport(BaseModel):
    checks: list[QualityCheck]
    warnings: int
    modules: list[ModuleStatus]
    generated_at: str
