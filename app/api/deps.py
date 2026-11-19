"""Dependency providers. Services are created once and reused (the research results live in one)."""

from functools import lru_cache

from app.core.config import get_settings
from app.core.openai_client import get_openai_client
from app.core.resources import load_allowed_domains, load_topics
from app.core.store import JsonFileStore, resolve_store_path
from app.services.best_practices_service import BestPracticesService, load_best_practice_domains
from app.services.benchmark_service import BenchmarkService, load_entities, load_indicators
from app.services.agent_common import make_store
from app.services.chat_service import ChatService
from app.services.excellence_service import ExcellenceService
from app.services.executive_service import ExecutiveService
from app.services.followup_service import FollowupService
from app.services.portfolio_service import PortfolioService
from app.services.process_service import ProcessService
from app.services.horizon_service import HorizonService
from app.services.trends_service import TrendsService


@lru_cache
def get_chat_service() -> ChatService:
    return ChatService(get_openai_client(), get_settings())


@lru_cache
def get_trends_service() -> TrendsService:
    settings = get_settings()
    path = resolve_store_path(settings.trends_store_path)
    store = JsonFileStore(path) if path else None
    return TrendsService(get_openai_client(), settings, load_allowed_domains(), load_topics(), store)


@lru_cache
def get_horizon_service() -> HorizonService:
    settings = get_settings()
    path = resolve_store_path(settings.horizon_store_path)
    store = JsonFileStore(path) if path else None
    return HorizonService(get_openai_client(), settings, get_trends_service(), load_topics(), store)


@lru_cache
def get_benchmark_service() -> BenchmarkService:
    settings = get_settings()
    path = resolve_store_path(settings.benchmark_store_path)
    store = JsonFileStore(path) if path else None
    entities, valid_types = load_entities()
    return BenchmarkService(
        get_openai_client(), settings, entities, valid_types, load_indicators(), store
    )


@lru_cache
def get_best_practices_service() -> BestPracticesService:
    settings = get_settings()
    path = resolve_store_path(settings.best_practices_store_path)
    store = JsonFileStore(path) if path else None
    return BestPracticesService(
        get_openai_client(), settings, get_horizon_service(), load_topics(), load_best_practice_domains(), store
    )


@lru_cache
def get_process_service() -> ProcessService:
    settings = get_settings()
    return ProcessService(get_openai_client(), settings, make_store(settings, "process_store.json"))


@lru_cache
def get_excellence_service() -> ExcellenceService:
    settings = get_settings()
    return ExcellenceService(get_openai_client(), settings, make_store(settings, "excellence_store.json"))


@lru_cache
def get_portfolio_service() -> PortfolioService:
    settings = get_settings()
    return PortfolioService(
        get_openai_client(), settings, make_store(settings, "portfolio_store.json"),
        best_practices=get_best_practices_service(), horizon=get_horizon_service(), topics=load_topics(),
    )


@lru_cache
def get_followup_service() -> FollowupService:
    settings = get_settings()
    return FollowupService(get_openai_client(), settings, make_store(settings, "followup_store.json"))


@lru_cache
def get_executive_service() -> ExecutiveService:
    return ExecutiveService(
        get_settings(),
        horizon=get_horizon_service(), benchmark=get_benchmark_service(), best_practices=get_best_practices_service(),
        process=get_process_service(), excellence=get_excellence_service(),
        portfolio=get_portfolio_service(), followup=get_followup_service(),
    )
