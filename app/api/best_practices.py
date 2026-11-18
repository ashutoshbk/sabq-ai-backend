from fastapi import APIRouter, Depends, Query

from app.api.deps import get_best_practices_service
from app.schemas.best_practices import (
    BestPracticesResponse,
    GapsResponse,
    PracticeChatRequest,
    PracticeChatResponse,
    RunRequest,
)
from app.services.best_practices_service import BestPracticesService

router = APIRouter(prefix="/agents/best-practices", tags=["Agents: Best Practices Curator"])


@router.get(
    "/gaps",
    response_model=GapsResponse,
    summary="The gaps the agent can find practices for (from Agent 1's trend cards and the platform data)",
)
async def list_gaps(service: BestPracticesService = Depends(get_best_practices_service)):
    return GapsResponse(gaps=service.gaps())


@router.get(
    "",
    response_model=BestPracticesResponse,
    summary="Practice cards for one gap (reads the saved results; searches only with refresh=true)",
)
async def get_practices(
    gap: str = Query(..., description="A gap id from /agents/best-practices/gaps, for example digital-twin."),
    refresh: bool = Query(False, description="Search the web again now (one search)."),
    service: BestPracticesService = Depends(get_best_practices_service),
):
    if refresh:
        return await service.run(gap_id=gap, refresh=True)
    return service.get(gap)


@router.post(
    "/run",
    response_model=BestPracticesResponse,
    summary="Find practices for a gap id, or for a gap described in your own words",
)
async def run(req: RunRequest, service: BestPracticesService = Depends(get_best_practices_service)):
    """Searches only if nothing is saved for this gap yet, or `refresh` is true."""
    return await service.run(gap_id=req.gap_id, custom_gap=req.custom_gap, refresh=req.refresh)


@router.post(
    "/chat",
    response_model=PracticeChatResponse,
    summary="Ask the agent for best practices in a short conversation",
)
async def chat(req: PracticeChatRequest, service: BestPracticesService = Depends(get_best_practices_service)):
    """If the request is vague the agent asks which gap (type=question, with suggested replies).
    Send the whole conversation back in `history` each time. Once it knows enough it returns
    type=result with the practice cards and a plan that says what it understood."""
    return await service.chat(req)
