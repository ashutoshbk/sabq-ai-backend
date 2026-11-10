import os

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.openai_client import get_openai_client

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("", summary="Is the service up, and is an API key configured?")
async def health(settings: Settings = Depends(get_settings)):
    return {
        "status": "ok",
        "openai_key_configured": bool(settings.openai_api_key or os.getenv("OPENAI_API_KEY")),
        "chat_model": settings.chat_model,
        "trends_model": settings.trends_model,
    }


@router.get("/openai", summary="Does the API key work, and do both models exist for it?")
async def health_openai(
    settings: Settings = Depends(get_settings), client=Depends(get_openai_client)
):
    """Cheap check: looks the models up. It sends no prompt, so it costs nothing."""
    results = {}
    for label, model in (("chat_model", settings.chat_model), ("trends_model", settings.trends_model)):
        try:
            await client.models.retrieve(model)
            results[label] = {"model": model, "ok": True}
        except Exception as exc:  # noqa: BLE001 - we want to report, not raise
            results[label] = {"model": model, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
    overall = "ok" if all(r["ok"] for r in results.values()) else "problem"
    return {"status": overall, **results}
