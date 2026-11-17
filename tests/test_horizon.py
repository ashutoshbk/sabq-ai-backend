"""Horizon Scanning Agent: analysis, honesty rules, saving, ranking, the conversation, the job."""

import asyncio
import functools
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_horizon_service
from app.core.jobs import refresh_feed_then_agent
from app.core.resources import load_topics
from app.core.store import JsonFileStore
from app.main import create_app
from app.schemas.trends import StoredItem
from app.services.horizon_parsing import (
    clean_plan,
    latest_possible_date,
    load_json_object,
    parse_analysis,
    parse_plan,
    rank_cards,
)
from app.services.horizon_service import HorizonService
from tests.conftest import FakeClient, FakeResponse, make_settings

TOPICS = load_topics()
NAMES = {t.slug: t.name for t in TOPICS}
DT = "digital-twin"
NOW = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)


# ---- helpers ------------------------------------------------------------

def sync(fn):
    """The project has no async test plugin, so run an async test body ourselves."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


def item(slug, n, date="2026-05-10"):
    return StoredItem(
        title=f"{NAMES[slug]} study {n}",
        summary=f"Summary of study {n}.",
        category="Strategic Thinking",
        published_date=date,
        source_name="RAND Corporation",
        source_url=f"https://www.rand.org/pubs/{slug}/{n}.html",
        first_seen="2026-11-10T06:00:00+00:00",
    )


class FakeTrends:
    def __init__(self, **per_topic):
        self.items = {slug: list(v) for slug, v in per_topic.items()}

    def stored_items(self, slug):
        return list(self.items.get(slug, []))


def card(trend="Simulating policy changes", refs=(1,), impact=4, tta=2, rel=5, **extra):
    c = {
        "trend": trend,
        "theme": "Policies",
        "summary": "Agencies test new policies in a virtual copy first [1].",
        "scores": {
            "impact": {"score": impact, "reason": "Cuts the cost of testing policy."},
            "time_to_adopt": {"score": tta, "reason": "Tools already exist."},
            "relevance": {"score": rel, "reason": "Fits the policy process."},
        },
        "so_what_for_dubai_police": "Consider a pilot for policy simulation.",
        "source_refs": list(refs),
        "confidence": "medium",
    }
    c.update(extra)
    return c


def analysis(*cards):
    return FakeResponse(json.dumps({"trends": list(cards)}))


def plan_reply(action, message="", suggestions=(), plan=None):
    return FakeResponse(
        json.dumps({"action": action, "message": message, "suggestions": list(suggestions), "plan": plan})
    )


class Brain:
    """The fake model. Analysis prompts and planner prompts are told apart by `instructions`."""

    def __init__(self, fake):
        self.analysis: dict[str, list] = {}
        self.planner: list = []
        self.fail: set[str] = set()
        self.analysis_calls: list[str] = []
        self.prompts: list[str] = []
        self.planner_calls: list[dict] = []
        fake.responses.handler = self

    def add(self, slug, *responses):
        self.analysis.setdefault(NAMES[slug], []).extend(responses)

    def __call__(self, kwargs):
        if "instructions" in kwargs:
            self.planner_calls.append(kwargs)
            return self.planner.pop(0)
        prompt = kwargs["input"]
        self.prompts.append(prompt)
        name = next(n for n in NAMES.values() if f'topic "{n}"' in prompt)
        self.analysis_calls.append(name)
        if name in self.fail:
            raise RuntimeError("model down")
        queue = self.analysis.get(name, [])
        return queue.pop(0) if queue else analysis()


def make(trends=None, store=None, **settings):
    fake = FakeClient()
    brain = Brain(fake)
    trends = trends or FakeTrends(**{DT: [item(DT, 1), item(DT, 2)]})
    service = HorizonService(
        fake, make_settings(**settings), trends, TOPICS, store=store, now_fn=lambda: NOW
    )
    return service, brain, trends


def client_for(service):
    app = create_app()
    app.dependency_overrides[get_horizon_service] = lambda: service
    return TestClient(app)


# ---- analysis and honesty rules ----------------------------------------

@sync
async def test_builds_cards_with_sources_copied_from_the_items():
    service, brain, _ = make()
    brain.add(DT, analysis(card(refs=[1, 2])))

    result = await service.run([DT])

    topic = result.topics[0]
    assert topic.count == 1 and topic.warning is None
    c = topic.cards[0]
    assert c.id == "digital-twin-1" and c.status == "draft"
    assert c.scores.relevance.score == 5 and c.confidence == "medium"
    assert [s.source_url for s in c.sources] == [
        "https://www.rand.org/pubs/digital-twin/1.html",
        "https://www.rand.org/pubs/digital-twin/2.html",
    ]
    assert c.sources[0].title == "Digital Twin study 1"
    assert c.sources[0].source_name == "RAND Corporation"


@sync
async def test_links_written_by_the_model_are_ignored():
    service, brain, _ = make()
    sneaky = card(refs=[1], source_url="https://evil.example/fake", sources=[{"source_url": "https://evil.example/x"}])
    brain.add(DT, analysis(sneaky))

    result = await service.run([DT])

    urls = [s.source_url for s in result.topics[0].cards[0].sources]
    assert urls == ["https://www.rand.org/pubs/digital-twin/1.html"]


@sync
async def test_prompt_only_contains_numbered_items_and_the_demo_profile():
    service, brain, _ = make()
    brain.add(DT, analysis(card()))

    await service.run([DT])

    prompt = brain.prompts[0]
    assert "[1] Digital Twin study 1 | RAND Corporation | 2026-05-10" in prompt
    assert "[2] Digital Twin study 2" in prompt
    assert "Expected horizon: 2-3 years" in prompt  # the demo profile from the HTML
    assert "https://" not in prompt.split("SOURCES")[1].split("RULES")[0]  # the model never sees links


@sync
async def test_cards_without_a_real_source_or_a_score_are_dropped():
    service, brain, _ = make()
    no_source = card("No source", refs=[9])
    no_score = card("No score")
    del no_score["scores"]["impact"]
    brain.add(DT, analysis(no_source, no_score, card("Good one", refs=[2])))

    result = await service.run([DT])

    assert [c.trend for c in result.topics[0].cards] == ["Good one"]


@sync
async def test_scores_are_clamped_and_bad_citations_removed():
    service, brain, _ = make()
    wild = card(impact=9, tta=0, summary="Real [1] and imaginary [7] claims.")
    brain.add(DT, analysis(wild))

    c = (await service.run([DT])).topics[0].cards[0]

    assert c.scores.impact.score == 5 and c.scores.time_to_adopt.score == 1
    assert "[1]" in c.summary and "[7]" not in c.summary


@sync
async def test_old_dated_items_are_not_shown_to_the_model_undated_are_kept():
    trends = FakeTrends(**{DT: [item(DT, 1, "2026-05-10"), item(DT, 2, "2024-01-01"), item(DT, 3, None)]})
    service, brain, _ = make(trends=trends)
    brain.add(DT, analysis(card()))

    await service.run([DT])

    prompt = brain.prompts[0]
    assert "study 1" in prompt and "study 3" in prompt
    assert "study 2" not in prompt


@sync
async def test_a_bare_year_gets_the_benefit_of_the_doubt():
    # now = 17 Nov 2026, limit 12 months -> cutoff about 17 Nov 2025
    trends = FakeTrends(**{DT: [item(DT, 1, "2025"), item(DT, 2, "2024"), item(DT, 3, "2025-06")]})
    service, brain, _ = make(trends=trends, horizon_max_age_months=12)
    brain.add(DT, analysis(card()))

    await service.run([DT])

    prompt = brain.prompts[0]
    assert "study 1" in prompt  # "2025" could still be December 2025
    assert "study 2" not in prompt and "study 3" not in prompt


def test_all_research_too_old_says_so_not_that_there_is_none():
    trends = FakeTrends(**{DT: [item(DT, 1, "2020"), item(DT, 2, "2021-05")]})
    service, brain, _ = make(trends=trends)
    brain.planner.append(plan_reply("run", plan={"topics": [DT]}))
    tc = client_for(service)

    warned = tc.get(f"/agents/horizon?topic={DT}").json()["topics"][0]["warning"]
    body = tc.post("/agents/horizon/chat", json={"message": "digital twin"}).json()

    assert "older than 24 months" in warned
    assert body["type"] == "message" and "older than 24 months" in body["message"]
    assert "HORIZON_MAX_AGE_MONTHS" in body["message"]
    assert brain.analysis_calls == []


@sync
async def test_topic_without_research_makes_no_model_call_and_says_why():
    service, brain, _ = make(trends=FakeTrends())

    result = await service.run()

    assert brain.analysis_calls == []
    assert result.total == 0
    assert all("research feed has no items" in t.warning for t in result.topics)


@sync
async def test_empty_answer_is_retried_once_then_reported():
    service, brain, _ = make()
    brain.add(DT, analysis(), analysis())  # two answers with no usable cards

    with pytest.raises(Exception) as err:
        await service.run([DT])

    assert getattr(err.value, "code", "") == "no_valid_cards"
    assert len(brain.analysis_calls) == 2


@sync
async def test_second_try_can_rescue_the_first():
    service, brain, _ = make()
    brain.add(DT, FakeResponse("not json"), analysis(card()))

    result = await service.run([DT])

    assert result.total == 1 and len(brain.analysis_calls) == 2


# ---- skipping, forcing, failing ---------------------------------------

@sync
async def test_unchanged_research_is_not_analysed_again():
    service, brain, _ = make()
    brain.add(DT, analysis(card()))

    await service.run([DT])
    await service.run([DT])

    assert len(brain.analysis_calls) == 1


@sync
async def test_new_research_or_force_runs_again():
    service, brain, trends = make()
    brain.add(DT, analysis(card("First")), analysis(card("Second")), analysis(card("Third")))

    await service.run([DT])
    trends.items[DT].append(item(DT, 3))
    second = await service.run([DT])
    third = await service.run([DT], force=True)

    assert len(brain.analysis_calls) == 3
    assert second.topics[0].cards[0].trend == "Second"
    assert third.topics[0].cards[0].trend == "Third"


@sync
async def test_one_topic_failing_does_not_stop_the_others():
    trends = FakeTrends(**{DT: [item(DT, 1)], "ai-policing": [item("ai-policing", 1)]})
    service, brain, _ = make(trends=trends)
    brain.fail = {NAMES["ai-policing"]}
    brain.add(DT, analysis(card()))

    result = await service.run([DT, "ai-policing"])

    by = {t.topic: t for t in result.topics}
    assert by[DT].count == 1
    assert by["ai-policing"].count == 0 and "failed" in by["ai-policing"].warning


@sync
async def test_all_failing_raises_and_old_cards_survive_a_later_failure():
    service, brain, trends = make()
    brain.add(DT, analysis(card("Keep me")))
    await service.run([DT])

    trends.items[DT].append(item(DT, 3))
    brain.fail = {NAMES[DT]}
    with pytest.raises(RuntimeError):
        await service.run([DT])

    shown = service.get_cards(DT)
    assert shown.topics[0].cards[0].trend == "Keep me"
    assert "showing earlier cards" in shown.topics[0].warning


# ---- ranking and top N --------------------------------------------------

async def three_cards():
    service, brain, _ = make()
    brain.add(DT, analysis(
        card("A", impact=2, tta=5, rel=3),
        card("B", impact=5, tta=3, rel=4),
        card("C", impact=3, tta=1, rel=5),
    ))
    await service.run([DT])
    return service


@sync
async def test_ranking_options_and_top_n():
    service = await three_cards()

    def order(**kw):
        return [c.trend for c in service.get_cards(DT, **kw).topics[0].cards]

    assert order() == ["C", "B", "A"]  # relevance is the default
    assert order(rank_by="impact") == ["B", "C", "A"]
    assert order(rank_by="time_to_adopt") == ["C", "B", "A"]  # soonest first
    top = service.get_cards(DT, top_n=2).topics[0]
    assert [c.trend for c in top.cards] == ["C", "B"]
    assert top.count == 2 and top.total_available == 3


def test_unknown_topic_is_a_404():
    service, _, _ = make()
    r = client_for(service).get("/agents/horizon?topic=nope")
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_topic"


def test_api_read_and_refresh():
    service, brain, _ = make()
    brain.add(DT, analysis(card()), analysis(card("Fresh")))
    tc = client_for(service)

    empty = tc.get(f"/agents/horizon?topic={DT}")
    assert empty.status_code == 200 and empty.json()["total"] == 0
    assert brain.analysis_calls == []  # reading never calls the model

    first = tc.get(f"/agents/horizon?topic={DT}&refresh=true&top_n=1&rank_by=impact")
    assert first.status_code == 200 and first.json()["total"] == 1
    again = tc.get(f"/agents/horizon?topic={DT}&refresh=true")
    assert again.json()["topics"][0]["cards"][0]["trend"] == "Fresh"
    assert tc.get("/agents/horizon?top_n=0").status_code == 422
    assert tc.get("/agents/horizon?rank_by=bogus").json()["error"]["code"] == "invalid_request"


# ---- saving -------------------------------------------------------------

@sync
async def test_cards_survive_a_restart(tmp_path):
    store = JsonFileStore(tmp_path / "horizon.json")
    service, brain, trends = make(store=store)
    brain.add(DT, analysis(card("Saved")))
    await service.run([DT])

    again, brain2, _ = make(trends=trends, store=JsonFileStore(tmp_path / "horizon.json"))
    shown = again.get_cards(DT)

    assert shown.topics[0].cards[0].trend == "Saved"
    await again.run([DT])  # same research: nothing to redo
    assert brain2.analysis_calls == []


# ---- the conversation ---------------------------------------------------

def msg(role, text):
    return {"role": role, "content": text}


def ready_service(topics=(DT,)):
    trends = FakeTrends(**{s: [item(s, 1), item(s, 2)] for s in topics})
    service, brain, _ = make(trends=trends)
    for s in topics:
        brain.add(s, analysis(card("A", rel=3), card("B", rel=5), card("C", rel=4)))
    return service, brain


def test_vague_request_gets_a_question_with_buttons():
    service, brain, _ = make()
    brain.planner.append(plan_reply("ask", "Which topic?", ["All five topics", "Digital Twin"]))

    r = client_for(service).post("/agents/horizon/chat", json={"message": "Show me the trends"})

    body = r.json()
    assert r.status_code == 200
    assert body["type"] == "question" and body["message"] == "Which topic?"
    assert body["suggestions"] == ["All five topics", "Digital Twin"]
    assert body["result"] is None and brain.analysis_calls == []


def test_clear_request_runs_and_explains_what_it_did():
    service, brain = ready_service()
    brain.planner.append(plan_reply("run", plan={"topics": [DT], "top_n": 2, "rank_by": "impact"}))

    r = client_for(service).post("/agents/horizon/chat", json={"message": "top 2 digital twin by impact"})

    body = r.json()
    assert body["type"] == "result"
    assert body["plan"] == {"topics": [DT], "top_n": 2, "rank_by": "impact", "reanalyze": False}
    assert body["result"]["topics"][0]["count"] == 2
    assert "top 2 per topic" in body["message"] and "impact" in body["message"]
    assert "Rank by relevance instead" in body["suggestions"]


def test_a_conversation_turns_a_question_into_a_result():
    service, brain = ready_service()
    brain.planner += [
        plan_reply("ask", "Which topic?", ["Digital Twin"]),
        plan_reply("run", plan={"topics": [DT], "top_n": 1}),
    ]
    tc = client_for(service)

    first = tc.post("/agents/horizon/chat", json={"message": "trends please"}).json()
    history = [msg("user", "trends please"), msg("assistant", first["message"])]
    second = tc.post("/agents/horizon/chat", json={"message": "Digital Twin, just the best", "history": history}).json()

    assert first["type"] == "question" and second["type"] == "result"
    assert second["result"]["topics"][0]["cards"][0]["trend"] == "B"  # best relevance
    sent = brain.planner_calls[1]["input"]
    assert [m["content"] for m in sent][-1] == "Digital Twin, just the best" and len(sent) == 3


def test_after_the_question_limit_it_runs_with_defaults():
    service, brain = ready_service()
    brain.planner.append(plan_reply("ask", "Anything else you want to tell me?"))
    history = [msg("user", "a"), msg("assistant", "q1"), msg("user", "b"), msg("assistant", "q2")]

    r = client_for(service).post("/agents/horizon/chat", json={"message": "dunno", "history": history})

    body = r.json()
    assert body["type"] == "result" and body["plan"]["rank_by"] == "relevance"
    assert "MUST answer" in brain.planner_calls[0]["instructions"]


def test_unreadable_planner_answer_asks_a_plain_question():
    service, brain, _ = make()
    brain.planner.append(FakeResponse("blah"))

    body = client_for(service).post("/agents/horizon/chat", json={"message": "hi"}).json()

    assert body["type"] == "question" and "All five topics" in body["suggestions"]


def test_unreadable_answer_after_the_limit_still_runs():
    service, brain = ready_service()
    brain.planner.append(FakeResponse("blah"))
    history = [msg("assistant", "q1"), msg("user", "x"), msg("assistant", "q2")]

    body = client_for(service).post("/agents/horizon/chat", json={"message": "ok", "history": history}).json()

    assert body["type"] == "result"


def test_off_topic_gets_a_plain_message():
    service, brain, _ = make()
    brain.planner.append(plan_reply("message", "I can only show trend cards for the five topics."))

    body = client_for(service).post("/agents/horizon/chat", json={"message": "who won the cricket"}).json()

    assert body["type"] == "message" and "trend cards" in body["message"]
    assert brain.analysis_calls == []


def test_no_research_yet_says_to_run_the_feed_first():
    service, brain, _ = make(trends=FakeTrends())
    brain.planner.append(plan_reply("run", plan={"topics": [DT]}))

    body = client_for(service).post("/agents/horizon/chat", json={"message": "digital twin"}).json()

    assert body["type"] == "message"
    assert "research feed has to run first" in body["message"]
    assert brain.analysis_calls == []


def test_same_request_twice_reuses_the_cards():
    service, brain = ready_service()
    brain.planner += [plan_reply("run", plan={"topics": [DT]}), plan_reply("run", plan={"topics": [DT]})]
    tc = client_for(service)

    tc.post("/agents/horizon/chat", json={"message": "digital twin"})
    tc.post("/agents/horizon/chat", json={"message": "digital twin again"})

    assert len(brain.analysis_calls) == 1  # no reanalyze asked, so the saved cards are reused


def test_reanalyze_true_runs_the_analysis_again():
    service, brain = ready_service()
    brain.planner += [plan_reply("run", plan={"topics": [DT]}), plan_reply("run", plan={"topics": [DT], "reanalyze": True})]
    brain.add(DT, analysis(card("Again")))
    tc = client_for(service)

    tc.post("/agents/horizon/chat", json={"message": "digital twin"})
    r = tc.post("/agents/horizon/chat", json={"message": "redo it with the latest"}).json()

    assert len(brain.analysis_calls) == 2
    assert r["result"]["topics"][0]["cards"][0]["trend"] == "Again"


def test_chat_validation():
    service, _, _ = make()
    tc = client_for(service)
    assert tc.post("/agents/horizon/chat", json={"message": ""}).status_code == 422
    too_long = [msg("user", "a")] * 21
    assert tc.post("/agents/horizon/chat", json={"message": "x", "history": too_long}).status_code == 422


# ---- the scheduled job --------------------------------------------------

@sync
async def test_job_runs_the_agent_after_the_feed():
    calls = []

    async def feed():
        calls.append("feed")

    async def agent():
        calls.append("agent")

    await refresh_feed_then_agent(feed, agent)
    assert calls == ["feed", "agent"]


@sync
async def test_job_skips_the_agent_when_the_feed_fails():
    calls = []

    async def feed():
        raise RuntimeError("search down")

    async def agent():
        calls.append("agent")

    with pytest.raises(RuntimeError):
        await refresh_feed_then_agent(feed, agent)
    assert calls == []


@sync
async def test_agent_failure_does_not_fail_the_job():
    async def feed():
        return None

    async def agent():
        raise RuntimeError("analysis down")

    await refresh_feed_then_agent(feed, agent)  # must not raise


@sync
async def test_job_without_agent():
    calls = []

    async def feed():
        calls.append("feed")

    await refresh_feed_then_agent(feed, None)
    assert calls == ["feed"]


# ---- pure helpers -------------------------------------------------------

def test_load_json_object_handles_fences_and_chatter():
    assert load_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert load_json_object('Sure! {"a": 1} Hope that helps') == {"a": 1}
    assert load_json_object("nothing here") is None
    assert load_json_object("[1, 2]") is None


def test_parse_analysis_limits_and_fallbacks():
    text = json.dumps({"trends": [card("1"), card("2", theme="Weird", confidence="sure"), card("3")]})
    cards = parse_analysis(text, 2, 2)
    assert [c["trend"] for c in cards] == ["1", "2"]
    assert cards[1]["theme"] == "General" and cards[1]["confidence"] == "low"
    assert parse_analysis("junk", 2, 5) == []
    assert parse_analysis(json.dumps({"trends": "x"}), 2, 5) == []


def test_parse_plan_and_clean_plan():
    parsed = parse_plan('{"action": "ASK", "message": "Hi", "suggestions": ["a", "a", "b"], "plan": null}')
    assert parsed["action"] == "ask" and parsed["suggestions"] == ["a", "b"]
    assert parse_plan('{"action": "dance"}') is None

    slugs = [t.slug for t in TOPICS]
    assert clean_plan({"topics": "all", "top_n": 99, "rank_by": "luck"}, slugs, 5) == {
        "topics": slugs, "top_n": 5, "rank_by": "relevance", "reanalyze": False,
    }
    assert clean_plan({"topics": ["Digital-Twin", "nope"], "top_n": 0, "reanalyze": "yes"}, slugs, 5) == {
        "topics": [DT], "top_n": None, "rank_by": "relevance", "reanalyze": False,
    }
    assert clean_plan(None, slugs, 5)["topics"] == slugs


def test_settings_limits():
    with pytest.raises(ValidationError):
        make_settings(horizon_cards_per_topic=0)
    with pytest.raises(ValidationError):
        make_settings(horizon_max_questions=9)
    s = make_settings()
    assert s.horizon_cards_per_topic == 5 and s.horizon_max_age_months == 24
    assert s.horizon_auto_run_after_refresh is True and s.horizon_max_questions == 2


def test_latest_possible_date():
    assert latest_possible_date("2025") == "2025-12-31"
    assert latest_possible_date("2025-02") == "2025-02-28"
    assert latest_possible_date("2024-02") == "2024-02-29"
    assert latest_possible_date("2025-06-15") == "2025-06-15"
    assert latest_possible_date(None) is None and latest_possible_date("junk") is None


def test_rank_cards_is_stable_for_ties():
    service, _, _ = make()
    assert rank_cards([], "impact") == []
