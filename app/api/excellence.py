from fastapi import APIRouter, Depends, Query

from app.api.deps import get_excellence_service
from app.schemas.excellence import AssessRequest, Assessment, AwardInfo
from app.services.excellence_service import ExcellenceService

router = APIRouter(prefix="/agents/excellence", tags=["Agents: Excellence Radar"])


@router.get("/awards", response_model=list[AwardInfo], summary="Awards and standards, with the deadline countdown (demo data)")
async def awards(service: ExcellenceService = Depends(get_excellence_service)):
    return service.awards()


@router.get("/assessment", response_model=Assessment, summary="The saved readiness assessment of one award (never calls the model)")
async def assessment(
    award: str = Query(..., description="An id from /agents/excellence/awards, for example efqm"),
    service: ExcellenceService = Depends(get_excellence_service),
):
    return service.get(award)


@router.post("/assess", response_model=Assessment, summary="Assess readiness: evidence matched to each criterion, gaps, countdown")
async def assess(req: AssessRequest, service: ExcellenceService = Depends(get_excellence_service)):
    """One model call, no web search. Criteria and the evidence library are demo data until real ones are loaded.
    A saved result is returned unless `refresh` is true. Sending a new `deadline` only updates the countdown."""
    return await service.assess(req.award_id, req.deadline, req.refresh)
