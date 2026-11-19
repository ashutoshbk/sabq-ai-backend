from fastapi import APIRouter, Depends, Query

from app.api.deps import get_process_service
from app.schemas.process import AnalyzeRequest, ProcessAnalysis, ProcessInfo
from app.services.process_service import ProcessService

router = APIRouter(prefix="/agents/process", tags=["Agents: Process & Procedure Analyst"])


@router.get("/processes", response_model=list[ProcessInfo], summary="Processes the agent can analyse (demo data)")
async def processes(service: ProcessService = Depends(get_process_service)):
    return service.processes()


@router.get("/analysis", response_model=ProcessAnalysis, summary="The saved analysis of one process (never calls the model)")
async def analysis(
    process: str = Query(..., description="A code from /agents/process/processes, for example OPS-001"),
    service: ProcessService = Depends(get_process_service),
):
    return service.get(process)


@router.post("/analyze", response_model=ProcessAnalysis, summary="Analyse an SOP: steps, waste, maturity, simpler flow")
async def analyze(req: AnalyzeRequest, service: ProcessService = Depends(get_process_service)):
    """Send a `process_code` of a demo SOP, or paste `sop_text`. One model call, no web search. A saved
    result is returned unless `refresh` is true. The result is a draft for the process owner to approve."""
    return await service.analyze(req.process_code, req.sop_text, req.title, req.refresh)
