from fastapi import APIRouter, Depends, Query

from app.api.deps import get_portfolio_service
from app.schemas.portfolio import EvaluateRequest, Evaluation, OpportunitiesResponse, PortfolioItem
from app.services.portfolio_service import PortfolioService

router = APIRouter(prefix="/agents/portfolio", tags=["Agents: Improvement Portfolio"])


@router.get("/items", response_model=list[PortfolioItem], summary="The existing portfolio (demo data)")
async def items(service: PortfolioService = Depends(get_portfolio_service)):
    return service.items()


@router.get("/opportunities", response_model=OpportunitiesResponse, summary="Opportunities from the saved Best Practices and Horizon Scan cards")
async def opportunities(service: PortfolioService = Depends(get_portfolio_service)):
    return OpportunitiesResponse(opportunities=service.opportunities())


@router.get("/evaluation", response_model=Evaluation, summary="The saved evaluation of one opportunity (never calls the model)")
async def evaluation(
    opportunity: str = Query(..., description="An id from /agents/portfolio/opportunities"),
    service: PortfolioService = Depends(get_portfolio_service),
):
    return service.get(opportunity)


@router.post("/evaluate", response_model=Evaluation, summary="Score an opportunity, check overlap, draft a charter, recommend")
async def evaluate(req: EvaluateRequest, service: PortfolioService = Depends(get_portfolio_service)):
    """One model call, no web search. Send an `opportunity_id`, or a `title` and `description`. A saved
    result is returned unless `refresh` is true. The committee decides: the result is a draft."""
    return await service.evaluate(req.opportunity_id, req.title, req.description, req.refresh)
