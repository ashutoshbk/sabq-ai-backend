from fastapi import APIRouter, Depends, Query

from app.api.deps import get_horizon_service
from app.schemas.horizon import HorizonChatRequest, HorizonChatResponse, HorizonResponse, RankBy
from app.services.horizon_service import HorizonService

router = APIRouter(prefix="/agents/horizon", tags=["Agents: Horizon Scanning"])


@router.get(
    "",
    response_model=HorizonResponse,
    summary="Scored trend cards (read the last results; no model call unless refresh=true)",
)
async def get_cards(
    topic: str | None = Query(None, description="Only this topic, for example digital-twin."),
    top_n: int | None = Query(None, ge=1, le=10, description="Only the best N cards per topic."),
    rank_by: RankBy = Query("relevance", description="relevance | impact | time_to_adopt (soonest first)"),
    refresh: bool = Query(
        False, description="Run the analysis again now on the stored research (one model call per topic)."
    ),
    service: HorizonService = Depends(get_horizon_service),
):
    topics = [topic] if topic else None
    if refresh:
        return await service.run(topics, force=True, top_n=top_n, rank_by=rank_by)
    return service.get_cards(topic, top_n, rank_by)


@router.post(
    "/chat",
    response_model=HorizonChatResponse,
    summary="Ask the agent for trend cards in a short conversation",
)
async def chat(req: HorizonChatRequest, service: HorizonService = Depends(get_horizon_service)):
    """If the request is vague the agent asks a question (type=question, with suggested replies).
    Send the whole conversation back in `history` each time. Once it knows enough it returns
    type=result with the cards and a plan that says what it understood."""
    return await service.chat(req)
