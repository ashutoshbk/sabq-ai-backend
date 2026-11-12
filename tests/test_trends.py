from datetime import date, datetime, timedelta, timezone

import httpx
import openai
import pytest

from app.core.resources import load_topics
from app.core.store import JsonFileStore
from app.services.trends_parsing import normalize_date
from tests.conftest import FakeResponse, trend_item, trends_json

NAMES = {t.slug: t.name for t in load_topics()}
DT = "digital-twin"


def url(slug, i, host="www.rand.org"):
    return f"https://{host}/pubs/{slug}/{i}.html"


def research(slug, numbers, **item_kw):
    """A fake search answer with one item per number, all cited."""
    urls = [url(slug, i) for i in numbers]
    items = [trend_item(u, title=f"{NAMES[slug]} study {i}", **item_kw) for u, i in zip(urls, numbers)]
    return FakeResponse(trends_json(*items), cited_urls=urls)


def rate_limit_error():
    req = httpx.Request("POST", "https://api.openai.com/v1/responses")
    return openai.RateLimitError("boom", response=httpx.Response(429, request=req), body=None)


class Script:
    """Answers each topic's search from its own list of prepared responses."""

    def __init__(self, fake):
        self.answers: dict[str, list] = {}
        self.fail: set[str] = set()
        self.topic_calls: list[str] = []
        fake.responses.handler = self

    def add(self, slug, *responses):
        self.answers.setdefault(NAMES[slug], []).extend(responses)

    def __call__(self, kwargs):
        prompt = kwargs["input"]
        name = next(n for n in NAMES.values() if f"Topic: {n}" in prompt)
        self.topic_calls.append(name)
        if name in self.fail:
            raise rate_limit_error()
        queue = self.answers.get(name, [])
        return queue.pop(0) if queue else FakeResponse(trends_json())


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)

    def now(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def topic_of(body, slug):
    return next(t for t in body["topics"] if t["topic"] == slug)


# ---- the basics ---------------------------------------------------------

def test_returns_all_five_topics(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    script = Script(fake)
    for slug in NAMES:
        script.add(slug, research(slug, [1, 2, 3]))

    r = tc.get("/trends")

    assert r.status_code == 200
    body = r.json()
    assert [t["topic"] for t in body["topics"]] == list(NAMES)
    assert body["total"] == 15 and body["cached"] is False and body["warning"] is None
    first = topic_of(body, DT)["items"][0]
    assert first["source_name"] == "RAND Corporation"  # from OUR list, not the model
    assert first["published_date"] == "2026-03-15"
    assert first["first_seen"] and first["id"] == 1
    assert set(script.topic_calls) == set(NAMES.values())


def test_search_uses_allowed_domains_and_the_topic_prompt(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    Script(fake).add(DT, research(DT, [1, 2, 3]))

    tc.get(f"/trends?topic={DT}")

    sent = fake.responses.calls[0]
    tool = sent["tools"][0]
    assert tool["type"] == "web_search"
    domains = tool["filters"]["allowed_domains"]
    assert "weforum.org" in domains and "rand.org" in domains and "arxiv.org" in domains
    assert sent["tool_choice"] == "required" and sent["store"] is False
    assert "Topic: Digital Twin" in sent["input"]
    assert "digital twins" in sent["input"]
    assert "research-grade" in sent["input"]


def test_single_topic_only_searches_that_topic(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    script = Script(fake)
    script.add(DT, research(DT, [1, 2, 3]))

    body = tc.get(f"/trends?topic={DT}").json()

    assert [t["topic"] for t in body["topics"]] == [DT]
    assert script.topic_calls == ["Digital Twin"]


def test_unknown_topic_is_a_clean_404(api):
    client, _ = api
    r = client.get("/trends?topic=cooking")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "unknown_topic"
    assert "digital-twin" in r.json()["error"]["message"]


def test_topics_endpoint(api):
    client, _ = api
    body = client.get("/trends/topics").json()
    assert [t["slug"] for t in body] == list(NAMES)
    assert body[2] == {"slug": "digital-twin", "name": "Digital Twin"}


# ---- trust checks -------------------------------------------------------

def test_unverifiable_items_are_dropped(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=1)
    good = url(DT, 1)
    invented = "https://www.rand.org/pubs/made-up.html"  # never cited by the search
    blog = "https://random-blog.example.com/post"  # cited, but not an allowed domain
    sneaky = "https://notrand.org/pubs/x.html"  # look-alike domain
    Script(fake).add(
        DT,
        FakeResponse(
            trends_json(trend_item(good, "Good"), trend_item(invented, "Invented"),
                        trend_item(blog, "Blog"), trend_item(sneaky, "Sneaky")),
            cited_urls=[good, blog, sneaky],
        ),
    )

    items = tc.get(f"/trends?topic={DT}").json()["topics"][0]["items"]

    assert [i["title"] for i in items] == ["Good"]


def test_tracking_parameters_are_removed(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=1)
    tracked = url(DT, 1) + "?utm_source=openai&trk=abc&id=7"
    Script(fake).add(DT, FakeResponse(trends_json(trend_item(tracked)), cited_urls=[tracked]))

    item = tc.get(f"/trends?topic={DT}").json()["topics"][0]["items"][0]

    assert item["source_url"] == url(DT, 1) + "?id=7"


def test_dates_are_kept_only_when_valid(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=4)
    urls = [url(DT, i) for i in range(1, 5)]
    dates = ["2026-05", "2031-01-01", "last spring", None]
    items = [trend_item(u, f"T{i}", published_date=d) for i, (u, d) in enumerate(zip(urls, dates), 1)]
    Script(fake).add(DT, FakeResponse(trends_json(*items), cited_urls=urls))

    got = {i["title"]: i["published_date"]
           for i in tc.get(f"/trends?topic={DT}").json()["topics"][0]["items"]}

    assert got == {"T1": "2026-05", "T2": None, "T3": None, "T4": None}


@pytest.mark.parametrize("value,expected", [
    ("2026-05-14", "2026-05-14"), ("2026-5-4", "2026-05-04"), ("2026-05", "2026-05"),
    ("2026", "2026"), ("2026-05-14T10:00:00Z", "2026-05-14"), ("2026-13-01", None),
    ("2026-02-30", None), ("1999-01-01", None), ("2099-01-01", None), ("May 2026", None),
    ("", None), (None, None),
])
def test_normalize_date(value, expected):
    assert normalize_date(value, date(2026, 11, 12)) == expected


# ---- many results, only new, ordering -----------------------------------

def test_fewer_than_wanted_is_not_padded(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=5)
    Script(fake).add(DT, research(DT, [1, 2]))  # second pass finds nothing more

    topic = tc.get(f"/trends?topic={DT}").json()["topics"][0]

    assert topic["count"] == 2 and topic["warning"] is None
    assert len(fake.responses.calls) == 2  # one search plus one "find more" pass


def test_second_pass_asks_for_more_and_avoids_pages_already_found(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=4)
    Script(fake).add(DT, research(DT, [1, 2]), research(DT, [3, 4]))

    topic = tc.get(f"/trends?topic={DT}").json()["topics"][0]

    assert topic["count"] == 4
    second_prompt = fake.responses.calls[1]["input"]
    assert "Do not return these pages again" in second_prompt
    assert url(DT, 1) in second_prompt


def test_keeps_only_the_newest_items_up_to_the_limit(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    dates = ["2026-01-01", "2026-04-01", "2026-02-01", None, "2025-12-01"]
    urls = [url(DT, i) for i in range(1, 6)]
    items = [trend_item(u, f"T{i}", published_date=d) for i, (u, d) in enumerate(zip(urls, dates), 1)]
    Script(fake).add(DT, FakeResponse(trends_json(*items), cited_urls=urls))

    got = tc.get(f"/trends?topic={DT}").json()["topics"][0]["items"]

    assert [i["published_date"] for i in got] == ["2026-04-01", "2026-02-01", "2026-01-01"]
    assert [i["id"] for i in got] == [1, 2, 3]


def test_a_refresh_only_adds_what_is_new(make_client):
    clock = Clock()
    tc, fake, _c, _t = make_client(now_fn=clock.now, trends_per_topic=5)
    script = Script(fake)
    script.add(DT, research(DT, [1, 2, 3]), FakeResponse(trends_json()))  # first run + its extra pass
    first = tc.get(f"/trends?topic={DT}").json()["topics"][0]
    assert first["count"] == 3 and first["new_in_last_refresh"] == 3
    first_seen = {i["title"]: i["first_seen"] for i in first["items"]}

    clock.advance(days=7)
    script.add(DT, research(DT, [3, 4, 5]))  # 3 is already known, 4 and 5 are new
    second = tc.get(f"/trends?topic={DT}&refresh=true").json()["topics"][0]

    assert second["count"] == 5 and second["new_in_last_refresh"] == 2
    assert second["last_changed"] == second["last_checked"] != first["last_checked"]
    for item in second["items"]:  # old items keep the time we first saw them
        if item["title"] in first_seen:
            assert item["first_seen"] == first_seen[item["title"]]

    clock.advance(days=7)
    script.add(DT, research(DT, [1, 2]))  # nothing new
    third = tc.get(f"/trends?topic={DT}&refresh=true").json()["topics"][0]

    assert third["count"] == 5 and third["new_in_last_refresh"] == 0
    assert third["last_changed"] == second["last_changed"]  # unchanged
    assert third["last_checked"] > second["last_checked"]  # but we did check


# ---- caching, staleness and failures ------------------------------------

def test_second_call_is_served_without_searching(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    Script(fake).add(DT, research(DT, [1, 2, 3]))

    first = tc.get(f"/trends?topic={DT}").json()
    calls = len(fake.responses.calls)
    second = tc.get(f"/trends?topic={DT}").json()

    assert first["cached"] is False and second["cached"] is True
    assert len(fake.responses.calls) == calls


def test_stale_results_are_refreshed_on_request(make_client):
    clock = Clock()
    tc, fake, _c, _t = make_client(now_fn=clock.now, trends_per_topic=3, trends_cache_ttl_minutes=60)
    script = Script(fake)
    script.add(DT, research(DT, [1, 2, 3]))
    tc.get(f"/trends?topic={DT}")
    calls = len(fake.responses.calls)

    clock.advance(minutes=30)
    tc.get(f"/trends?topic={DT}")
    assert len(fake.responses.calls) == calls  # still fresh

    clock.advance(hours=2)
    script.add(DT, research(DT, [4, 5, 6]))
    body = tc.get(f"/trends?topic={DT}").json()
    assert len(fake.responses.calls) > calls  # stale: searched again
    assert body["cached"] is False


def test_a_failed_refresh_keeps_earlier_results_and_backs_off(make_client):
    clock = Clock()
    tc, fake, _c, _t = make_client(now_fn=clock.now, trends_per_topic=3, trends_cache_ttl_minutes=60)
    script = Script(fake)
    script.add(DT, research(DT, [1, 2, 3]))
    tc.get(f"/trends?topic={DT}")

    clock.advance(hours=2)
    script.fail.add("Digital Twin")
    r = tc.get(f"/trends?topic={DT}")
    assert r.status_code == 200
    topic = r.json()["topics"][0]
    assert topic["count"] == 3 and "failed" in topic["warning"]

    calls = len(fake.responses.calls)
    tc.get(f"/trends?topic={DT}")  # right after a failure: no new attempt
    assert len(fake.responses.calls) == calls


def test_one_failing_topic_does_not_break_the_others(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    script = Script(fake)
    script.fail.add("Digital Twin")
    for slug in NAMES:
        script.add(slug, research(slug, [1, 2, 3]))

    body = tc.get("/trends").json()

    assert topic_of(body, DT)["count"] == 0 and "failed" in topic_of(body, DT)["warning"]
    assert topic_of(body, "ai-policing")["count"] == 3
    assert body["total"] == 12


def test_every_topic_failing_returns_the_openai_error(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    script = Script(fake)
    script.fail.update(NAMES.values())

    r = tc.get("/trends")

    assert r.status_code == 429
    assert r.json()["error"]["code"] == "openai_rate_limited"


def test_nothing_verifiable_anywhere_is_a_clean_502(make_client):
    tc, fake, _c, _t = make_client(trends_per_topic=3)
    Script(fake)  # every search comes back empty

    r = tc.get(f"/trends?topic={DT}")

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "no_valid_trends"


# ---- saving to a file ----------------------------------------------------

def test_results_survive_a_restart(make_client, tmp_path):
    path = tmp_path / "data" / "store.json"
    tc, fake, _c, _t = make_client(store=JsonFileStore(path), trends_per_topic=3)
    Script(fake).add(DT, research(DT, [1, 2, 3]))
    first = tc.get(f"/trends?topic={DT}").json()["topics"][0]
    assert path.exists() and "last_error" not in path.read_text()

    calls = len(fake.responses.calls)
    tc2, _f, _c2, _t2 = make_client(store=JsonFileStore(path), trends_per_topic=3)  # "restart"
    again = tc2.get(f"/trends?topic={DT}").json()

    assert len(fake.responses.calls) == calls  # served from the file, no search
    assert again["cached"] is True
    assert [i["source_url"] for i in again["topics"][0]["items"]] == [i["source_url"] for i in first["items"]]
    assert again["topics"][0]["items"][0]["first_seen"] == first["items"][0]["first_seen"]


def test_a_damaged_file_is_ignored(make_client, tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{ this is not json", encoding="utf-8")
    tc, fake, _c, _t = make_client(store=JsonFileStore(path), trends_per_topic=3)
    Script(fake).add(DT, research(DT, [1, 2, 3]))

    assert tc.get(f"/trends?topic={DT}").json()["topics"][0]["count"] == 3
    assert "topics" in path.read_text()  # and it was rewritten properly
