from fastapi import APIRouter, Depends, Query

from app.api.deps import get_trends_service
from app.core.resources import load_topics
from app.schemas.trends import TopicInfo, TrendsResponse
from app.services.trends_service import TrendsService

router = APIRouter(prefix="/trends", tags=["Trends"])


@router.get(
    "",
    response_model=TrendsResponse,
    summary="Research on the five global trends (up to 10 items per topic)",
)
async def get_trends(
    topic: str | None = Query(
        None, description="Only this topic, for example digital-twin. See GET /trends/topics."
    ),
    refresh: bool = Query(
        False, description="Run a new search now (costs several searches). Normally not needed."
    ),
    service: TrendsService = Depends(get_trends_service),
):
    return await service.get_trends(topic=topic, refresh=refresh)


@router.get(
    "/topics",
    response_model=list[TopicInfo],
    summary="The five topics (use the slug in ?topic=)",
)
async def list_topics():
    return [TopicInfo(slug=t.slug, name=t.name) for t in load_topics()]
