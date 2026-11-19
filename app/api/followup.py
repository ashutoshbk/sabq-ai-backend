from fastapi import APIRouter, Depends, Query

from app.api.deps import get_followup_service
from app.schemas.followup import CheckRequest, Digest, InitiativeInfo, StatusCheck
from app.services.followup_service import FollowupService

router = APIRouter(prefix="/agents/followup", tags=["Agents: Follow-up & Progress"])


@router.get("/initiatives", response_model=list[InitiativeInfo], summary="Approved initiatives with milestones (demo data)")
async def initiatives(service: FollowupService = Depends(get_followup_service)):
    return service.initiatives()


@router.get("/status", response_model=StatusCheck, summary="The latest saved check of one initiative (never calls the model)")
async def status(
    initiative: str = Query(..., description="A code from /agents/followup/initiatives, for example PRT-001"),
    service: FollowupService = Depends(get_followup_service),
):
    return service.get(initiative)


@router.post("/check", response_model=StatusCheck, summary="Check an owner's update and uploaded files against the milestones")
async def check(req: CheckRequest, service: FollowupService = Depends(get_followup_service)):
    """One model call, no web search. A milestone counts as done only if it cites an uploaded file that
    exists. Progress, status, delay risk and the reminder message are computed in code. Nothing is sent."""
    return await service.check(req)


@router.get("/digest", response_model=Digest, summary="Weekly digest of all initiatives for the manager (built in code, no model)")
async def digest(service: FollowupService = Depends(get_followup_service)):
    return service.digest()
