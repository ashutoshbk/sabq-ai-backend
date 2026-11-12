"""Sabq AI backend (POC): a chatbot and a research feed on five global trends.

Run:  uvicorn app.main:app --reload --port 8000
Docs: http://127.0.0.1:8000/docs
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import chat, health, trends
from app.api.deps import get_trends_service
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.scheduler import ScheduledRefresher, resolve_timezone

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.trends_auto_refresh_enabled:
            hour, minute = settings.refresh_hour_minute

            async def refresh() -> None:
                await get_trends_service().get_trends(refresh=True)

            refresher = ScheduledRefresher(
                refresh,
                hour,
                minute,
                tz=resolve_timezone(settings.trends_refresh_timezone),
                tz_name=settings.trends_refresh_timezone,
                weekday=settings.refresh_weekday,
                run_on_startup=settings.trends_refresh_on_startup,
            )
            app.state.trends_refresher = refresher
            task = asyncio.create_task(refresher.run(), name="trends-auto-refresh")
            task.add_done_callback(
                lambda t: logger.error("Auto refresh stopped: %r", t.exception())
                if not t.cancelled() and t.exception()
                else None
            )
            when = (
                f"every {settings.trends_refresh_day}"
                if settings.refresh_weekday is not None
                else "every day"
            )
            logger.info(
                "Automatic research refresh enabled: %s at %s %s",
                when, settings.trends_refresh_time, settings.trends_refresh_timezone,
            )
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="Sabq AI Backend (POC)",
        version="0.3.0",
        description=(
            "AI endpoints for the Sabq Smart Platform proof of concept.\n\n"
            "- **Chat**: a guard-railed assistant for strategic planning, foresight, "
            "performance, standards, awards and policing practices. No internal data.\n"
            "- **Trends**: research on five global trends in security and policing "
            "(Predictive Policing, AI Policing, Digital Twin, Autonomous Surveillance, "
            "Smart Investigation), up to 10 items per topic, found with OpenAI web search "
            "and restricted to trusted sources. Refreshed automatically, daily or weekly."
        ),
        lifespan=lifespan,
    )
    app.state.trends_refresher = None

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
