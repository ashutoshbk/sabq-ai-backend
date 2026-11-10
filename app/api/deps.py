"""Dependency providers. Services are created once and reused (the news cache lives in one)."""

from functools import lru_cache

from app.core.config import get_settings
from app.core.openai_client import get_openai_client
from app.core.resources import load_allowed_domains
from app.services.chat_service import ChatService
from app.services.trends_service import TrendsService


@lru_cache
def get_chat_service() -> ChatService:
    return ChatService(get_openai_client(), get_settings())


@lru_cache
def get_trends_service() -> TrendsService:
    return TrendsService(get_openai_client(), get_settings(), load_allowed_domains())
