from fastapi import APIRouter, Depends, Query

from app.api.deps import get_trends_service
from app.schemas.trends import TrendsResponse
from app.services.trends_service import TrendsService

router = APIRouter(prefix="/trends", tags=["Trends"])


@router.get(
    "",
    response_model=TrendsResponse,
    summary="Top 3 global trends in security and policing technology",
)
async def get_trends(
    refresh: bool = Query(False, description="Skip the cache and fetch fresh results."),
    service: TrendsService = Depends(get_trends_service),
):
    return await service.get_trends(refresh=refresh)
