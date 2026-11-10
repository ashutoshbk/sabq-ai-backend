import httpx
import openai

from app.core.errors import openai_error_to_response
from tests.conftest import FakeResponse


def _status_error(cls, status):
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    resp = httpx.Response(status, request=req)
    return cls("boom", response=resp, body=None)


def test_health(api):
    client, _ = api
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "openai_key_configured" in body
    assert "OPENAI" not in str(body).replace("openai_key_configured", "")  # never leaks a key


def test_root(api):
    client, _ = api
    assert client.get("/").json()["docs"] == "/docs"


def test_openapi_lists_our_endpoints(api):
    client, _ = api
    paths = client.get("/openapi.json").json()["paths"]
    for p in ["/chat", "/chat/stream", "/chat/quick-questions", "/trends", "/health", "/health/openai"]:
        assert p in paths


def test_cors_header_present(api):
    client, _ = api
    r = client.get("/health", headers={"Origin": "http://localhost:3000"})
    assert r.headers.get("access-control-allow-origin") in ("*", "http://localhost:3000")


def test_openai_error_mapping():
    cases = [
        (_status_error(openai.AuthenticationError, 401), 502, "openai_auth_error"),
        (_status_error(openai.RateLimitError, 429), 429, "openai_rate_limited"),
        (_status_error(openai.NotFoundError, 404), 502, "openai_model_not_found"),
        (_status_error(openai.BadRequestError, 400), 502, "openai_bad_request"),
        (_status_error(openai.InternalServerError, 500), 502, "openai_error"),
    ]
    for exc, status, code in cases:
        resp = openai_error_to_response(exc)
        assert resp.status_code == status
        assert code in resp.body.decode()


def test_timeout_and_connection_mapping():
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    assert openai_error_to_response(openai.APITimeoutError(request=req)).status_code == 504
    assert openai_error_to_response(openai.APIConnectionError(request=req)).status_code == 503


def test_openai_failure_surfaces_as_clean_json(api):
    """An exception raised by the client inside an endpoint becomes our error shape."""
    client, fake = api

    async def boom(**_):
        raise _status_error(openai.RateLimitError, 429)

    fake.responses.create = boom
    r = client.post("/chat", json={"message": "hi"})

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "openai_rate_limited"


class _FakeModels:
    def __init__(self, missing=()):
        self.missing = set(missing)

    async def retrieve(self, model):
        if model in self.missing:
            raise _status_error(openai.NotFoundError, 404)
        return {"id": model}


def _client_with_models(models):
    from fastapi.testclient import TestClient

    from app.core.openai_client import get_openai_client
    from app.main import create_app

    app = create_app()
    fake = type("C", (), {})()
    fake.models = models
    app.dependency_overrides[get_openai_client] = lambda: fake
    return TestClient(app)


def test_health_openai_all_good():
    with _client_with_models(_FakeModels()) as tc:
        body = tc.get("/health/openai").json()
    assert body["status"] == "ok"
    assert body["chat_model"]["ok"] and body["trends_model"]["ok"]


def test_health_openai_reports_the_model_that_is_missing():
    from app.core.config import get_settings

    missing = get_settings().trends_model
    with _client_with_models(_FakeModels(missing=[missing])) as tc:
        body = tc.get("/health/openai").json()
    assert body["status"] == "problem"
    assert body["trends_model"]["ok"] is False
    assert "NotFoundError" in body["trends_model"]["error"]
