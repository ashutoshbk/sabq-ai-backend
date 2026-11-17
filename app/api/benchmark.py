from fastapi import APIRouter, Depends, Query

from app.api.deps import get_benchmark_service
from app.schemas.benchmark import BenchmarkIndicator, BenchmarkResponse
from app.services.benchmark_service import BenchmarkService

router = APIRouter(prefix="/agents/benchmark", tags=["Agents: Benchmarking"])


@router.get(
    "",
    response_model=BenchmarkResponse,
    summary="Published indicator figures for the competitive table (saved results; no search unless refresh=true)",
)
async def get_benchmark(
    refresh: bool = Query(
        False,
        description="Search the web again now (about one search per indicator). Normally not needed: "
        "the scheduled run does it when the results are older than BENCHMARK_REFRESH_DAYS.",
    ),
    service: BenchmarkService = Depends(get_benchmark_service),
):
    if refresh:
        return await service.run(force=True)
    return service.get()


@router.get(
    "/indicators",
    response_model=list[BenchmarkIndicator],
    summary="The indicators the agent looks for (names and trusted domains, no links)",
)
async def list_indicators(service: BenchmarkService = Depends(get_benchmark_service)):
    return service.indicators()
