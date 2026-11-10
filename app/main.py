"""Sabq AI backend (POC): a chatbot and a live top-3 news feed.

Run:  uvicorn app.main:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, health, trends
from app.core.config import get_settings
from app.core.errors import register_error_handlers


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = FastAPI(
        title="Sabq AI Backend (POC)",
        version="0.1.0",
        description=(
            "AI endpoints for the Sabq Smart Platform proof of concept.\n\n"
            "- **Chat**: a guard-railed assistant for strategic planning, foresight, "
            "performance, standards, awards and policing practices. No internal data.\n"
            "- **Trends**: the top 3 global security and policing technology trends, "
            "found with OpenAI web search and restricted to trusted sources."
        ),
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)

    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(trends.router)

    @app.get("/", include_in_schema=False)
    async def root():
        return {"service": "sabq-ai-backend", "docs": "/docs", "health": "/health"}

    return app


app = create_app()
