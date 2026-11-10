from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_chat_service
from app.core.resources import load_quick_questions
from app.schemas.chat import ChatRequest, ChatResponse, QuickQuestionsResponse
from app.services.chat_service import ChatService

router = APIRouter(prefix="/chat", tags=["Chat"])


@router.post("", response_model=ChatResponse, summary="Send a message, get the full reply")
async def chat(req: ChatRequest, service: ChatService = Depends(get_chat_service)):
    return await service.reply(req)


@router.post(
    "/stream",
    summary="Send a message, get the reply as a stream (Server-Sent Events)",
    response_class=StreamingResponse,
)
async def chat_stream(req: ChatRequest, service: ChatService = Depends(get_chat_service)):
    stream = await service.open_stream(req)
    return StreamingResponse(
        service.iter_sse(stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/quick-questions",
    response_model=QuickQuestionsResponse,
    summary="The three fixed quick-question buttons",
)
async def quick_questions():
    return QuickQuestionsResponse(questions=load_quick_questions())
