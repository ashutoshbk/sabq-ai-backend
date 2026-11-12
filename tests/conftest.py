"""Shared test helpers: a fake OpenAI client so no test touches the network."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

# Tests must never start the real automatic refresh (it would call OpenAI)
# and must never write the results file.
os.environ["TRENDS_AUTO_REFRESH_ENABLED"] = "false"
os.environ["TRENDS_STORE_PATH"] = ""

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_chat_service, get_trends_service
from app.core.config import Settings
from app.core.resources import load_allowed_domains, load_topics
from app.main import create_app
from app.services.chat_service import ChatService
from app.services.trends_service import TrendsService


class FakeResponse:
    """Looks like an OpenAI Responses API result for what our code reads."""

    def __init__(self, text: str, cited_urls: list[str] | None = None, status: str = "completed"):
        self.output_text = text
        self.status = status
        self._cited = cited_urls or []

    def model_dump(self) -> dict:
        annotations = [{"type": "url_citation", "url": u, "title": "t"} for u in self._cited]
        return {
            "output": [
                {"type": "web_search_call", "action": {"sources": [{"type": "url", "url": u} for u in self._cited]}},
                {"type": "message", "content": [{"type": "output_text", "text": self.output_text, "annotations": annotations}]},
            ]
        }


class FakeStream:
    def __init__(self, events):
        self._events = events

    def __aiter__(self):
        async def gen():
            for e in self._events:
                yield e

        return gen()


class FakeResponses:
    def __init__(self):
        self.calls: list[dict] = []
        self.queue: list = []
        self.handler = None  # optional: a function(kwargs) -> response (it may raise)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.handler is not None:
            return self.handler(kwargs)
        if not self.queue:
            raise AssertionError("FakeResponses queue is empty")
        return self.queue.pop(0)


class FakeClient:
    def __init__(self):
        self.responses = FakeResponses()


def make_settings(**overrides) -> Settings:
    base = dict(openai_api_key="test-key", trends_cache_ttl_minutes=60)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def trends_json(*items: dict) -> str:
    return json.dumps({"items": list(items)})


def trend_item(url: str, title: str = "A study", **extra) -> dict:
    item = {
        "title": title,
        "summary": "A short summary in the model's own words.",
        "category": "Strategic Thinking",
        "published_date": "2026-03-15",
        "source_url": url,
    }
    item.update(extra)
    return item


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def make_client(fake_client):
    """Returns a (TestClient, FakeClient, chat_service, trends_service) bundle factory."""
    clients = []

    def _make(store=None, now_fn=None, **settings_overrides):
        settings = make_settings(**settings_overrides)
        chat = ChatService(fake_client, settings)
        trends = TrendsService(
            fake_client, settings, load_allowed_domains(), load_topics(), store=store, now_fn=now_fn
        )
        app = create_app()
        app.dependency_overrides[get_chat_service] = lambda: chat
        app.dependency_overrides[get_trends_service] = lambda: trends
        tc = TestClient(app)
        clients.append(tc)
        return tc, fake_client, chat, trends

    yield _make
    for c in clients:
        c.close()


@pytest.fixture
def api(make_client):
    tc, fake, _chat, _trends = make_client()
    return tc, fake


def stream_event(etype: str, **kwargs):
    return SimpleNamespace(type=etype, **kwargs)
