from fastapi import APIRouter, Depends

from app.api.deps import get_executive_service
from app.schemas.executive import DataQualityReport, ExecutiveBrief
from app.services.executive_service import ExecutiveService

router = APIRouter(prefix="/agents/executive", tags=["Agents: Executive Summary & Data Quality"])


@router.get("/brief", response_model=ExecutiveBrief, summary="One-page executive brief from the saved results of Agents 1 to 7")
async def brief(service: ExecutiveService = Depends(get_executive_service)):
    """Built in code (no model, no web search). At most 250 words. Every line names its source module."""
    return service.brief()


@router.get("/data-quality", response_model=DataQualityReport, summary="Data-quality checks and the freshness of every module")
async def data_quality(service: ExecutiveService = Depends(get_executive_service)):
    return service.data_quality()
