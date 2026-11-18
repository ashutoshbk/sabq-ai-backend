"""Best Practices Curator: gaps, what it keeps, ranking, saving, the conversation, the API."""

import asyncio
import functools
import json
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_best_practices_service
from app.core.resources import load_topics
from app.core.store import JsonFileStore
from app.main import create_app
from app.schemas.best_practices import Practice
from app.services.best_practices_parsing import (
    build_recommendation,
    clean_custom_gap,
    clean_plan,
    custom_gap_id,
    drop_repeated_results,
    effort_of,
    parse_plan,
    parse_practices,
    rank_key,
)
from app.services.best_practices_service import BestPracticesService, load_best_practice_domains
from tests.conftest import FakeClient, FakeResponse, make_settings

TOPICS = load_topics()
DOMAINS = load_best_practice_domains()
DT = "digital-twin"
NOW = datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc)
URL_A = "https://www.unodc.org/unodc/en/case-studies/dt-1.html"
URL_B = "https://www.rand.org/pubs/research_reports/rr-2.html"
URL_C = "https://www.police.gov.sg/media-room/news/2025-aipolicing"


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- fakes ----------------------------------------------------------------

def card(trend, relevance=4):
    score = SimpleNamespace(score=relevance, reason="r")
    return SimpleNamespace(
        trend=trend, so_what_for_dubai_police=f"So what for {trend}", scores=SimpleNamespace(relevance=score)
    )


class FakeHorizon:
    """Stands in for Agent 1: cards per topic slug."""

    def __init__(self, **per_topic):
        self.per_topic = per_topic

    def get_cards(self, topic, top_n=None, rank_by="relevance"):
        return SimpleNamespace(topics=[SimpleNamespace(cards=self.per_topic.get(topic, []))])


def practice(url=URL_A, **extra):
    p = {
        "organization": "UNODC", "country": "Austria", "title": "Digital twin for policy testing",
        "what_they_did": "Simulated new policies before rollout.", "result": "Cut pilot costs by 30% in 2024.",
        "result_kind": "achieved",
        "result_year": 2024, "evidence": "reduced costs by 30%", "prerequisites": "Clean data feeds",
        "effort": "M", "expected_benefit": "Lower pilot cost", "adaptation_for_dubai_police": "Link to policy team",
        "risks": "Data privacy", "source_url": url, "confidence": "high",
    }
    p.update(extra)
    return p


def answer(*practices, cited=None):
    cited = [p["source_url"] for p in practices] if cited is None else cited
    return FakeResponse(json.dumps({"practices": list(practices)}), cited)


class Brain:
    """Answers the search calls (they have tools) and the planner calls (they do not)."""

    def __init__(self, fake):
        self.searches: list[dict] = []
        self.plans: list[dict] = []
        self.search_queue: list = []
        self.plan_queue: list = []
        fake.responses.handler = self

    def __call__(self, kwargs):
        if kwargs.get("tools"):
            self.searches.append(kwargs)
            return self.search_queue.pop(0) if self.search_queue else answer()
        self.plans.append(kwargs)
        return self.plan_queue.pop(0)


def plan_answer(action, message="", plan=None, suggestions=None):
    return FakeResponse(json.dumps({"action": action, "message": message, "suggestions": suggestions or [], "plan": plan}))


def make(horizon=None, store=None, context=None, **settings):
    fake = FakeClient()
    brain = Brain(fake)
    service = BestPracticesService(
        fake, make_settings(**settings),
        horizon or FakeHorizon(**{DT: [card("Digital twins of security systems", 5), card("Policy simulation", 3)]}),
        TOPICS, DOMAINS, store=store, context=context, now_fn=lambda: NOW,
    )
    return service, brain


def client_for(service):
    app = create_app()
    app.dependency_overrides[get_best_practices_service] = lambda: service
    return TestClient(app)


# ---- the gaps -----------------------------------------------------------------

def test_a_gap_is_built_from_agent_1_cards_and_platform_data():
    service, _ = make()
    gap = next(g for g in service.gaps() if g.id == DT)

    assert gap.origin == "trend" and gap.topic_name == "Digital Twin"
    assert gap.based_on == ["Digital twins of security systems", "Policy simulation"]
    assert "So what for Policy simulation" in gap.description
    assert "Platform demo data" in gap.platform_context and gap.strategic_gap_percent == 12


def test_topics_with_neither_cards_nor_platform_data_have_no_gap():
    service, _ = make(horizon=FakeHorizon())
    ids = [g.id for g in service.gaps()]
    assert set(ids) == {"predictive-policing", "ai-policing", "digital-twin"}  # the three with a platform profile


def test_gaps_are_ordered_by_the_platform_gap_percent():
    service, _ = make(horizon=FakeHorizon())
    percents = [g.strategic_gap_percent for g in service.gaps()]
    assert percents == sorted(percents, reverse=True)
    assert [g.priority for g in service.gaps()] == [1, 2, 3]


def test_a_failing_agent_1_does_not_break_the_gaps():
    class Broken:
        def get_cards(self, *a, **k):
            raise RuntimeError("down")

    service, _ = make(horizon=Broken())
    assert {g.id for g in service.gaps()} == {"predictive-policing", "ai-policing", "digital-twin"}


def test_a_trend_gap_without_platform_data_has_no_platform_context():
    service, _ = make(horizon=FakeHorizon(**{"autonomous-surveillance": [card("Drone patrols")]}), context={})
    gaps = service.gaps()
    assert [g.id for g in gaps] == ["autonomous-surveillance"]
    assert gaps[0].platform_context is None and gaps[0].strategic_gap_percent is None


# ---- what is kept ----------------------------------------------------------------

def parse(*items, cited=None):
    resp = answer(*items, cited=cited)
    return parse_practices(resp.output_text, resp.model_dump(), DOMAINS, date(2026, 11, 18))


def test_a_good_practice_is_kept_with_our_own_source_name():
    kept = parse(practice())
    assert len(kept) == 1
    assert kept[0]["source_name"] == "UN Office on Drugs and Crime (UNODC)"
    assert kept[0]["effort"] == "M" and kept[0]["result_year"] == 2024


def test_a_practice_whose_page_was_not_cited_is_dropped():
    assert parse(practice(), cited=[]) == []


def test_a_practice_on_an_untrusted_domain_is_dropped():
    bad = "https://blog.example.com/dt"
    assert parse(practice(url=bad), cited=[bad]) == []


def test_a_look_alike_domain_is_dropped():
    bad = "https://unodc.org.evil.com/dt"
    assert parse(practice(url=bad), cited=[bad]) == []


def test_a_practice_without_a_measurable_result_is_dropped():
    assert parse(practice(result="")) == []
    assert parse(practice(what_they_did="")) == []


def test_duplicates_are_removed_and_a_future_year_is_ignored():
    kept = parse(practice(), practice(url=URL_B), practice(title="Other", result_year=2031, url=URL_B))
    assert len(kept) == 2
    assert next(p for p in kept if p["title"] == "Other")["result_year"] is None


def test_funding_plans_and_unproven_benefits_are_not_results():
    assert parse(practice(result_kind="none")) == []
    assert parse(practice(result_kind="")) == []
    assert parse(practice(result_kind=None)) == []
    assert parse(practice(result="AI can improve response times, but benefits need more evidence.")) == []  # no number
    assert len(parse(practice(result="Saved around a million hours a year."))) == 1  # a number written in words


def test_money_put_in_is_not_a_result_but_money_saved_is():
    assert parse(practice(result="Supported police adopt AI with over £50m of investment.")) == []
    assert parse(practice(result="Funded with $2 million.")) == []
    assert len(parse(practice(result="Annual efficiency savings of £250,000 for one force."))) == 1
    assert len(parse(practice(result="A £5m programme cut case time by 20%."))) == 1


def test_an_estimate_is_kept_but_labelled():
    kept = parse(practice(result="Estimated to save about a million hours a year if adopted.", result_kind="Estimated"))
    assert kept[0]["result_kind"] == "estimated"


def test_the_same_result_from_the_same_page_is_shown_once():
    a = practice(title="A", result="800 hours of footage reviewed in 3 hours.", confidence="high")
    b = practice(title="B", result="Early trials: 800 hours reviewed in 3 hours and 500,000 e-books translated.", confidence="medium")
    c = practice(title="C", result="Frees up 3,000 officers.", url=URL_A)
    d = practice(title="D", result="800 hours reviewed in 3 hours.", url=URL_B)  # another page: kept
    kept = drop_repeated_results(parse(a, b, c, d, cited=[URL_A, URL_B]))
    assert [k["title"] for k in kept] == ["A", "C", "D"]


def test_unreadable_text_is_not_the_same_as_nothing_found():
    assert parse_practices("not json", {}, DOMAINS, date(2026, 11, 18)) is None
    assert parse_practices('{"practices": []}', {}, DOMAINS, date(2026, 11, 18)) == []


def test_effort_words_are_understood_and_unknown_effort_is_not_guessed():
    assert effort_of("small") == "S" and effort_of("L") == "L" and effort_of("Medium") == "M"
    assert effort_of("unknown") is None and effort_of(None) is None
    assert parse(practice(effort="unclear"))[0]["effort"] is None


# ---- ranking and recommendation -------------------------------------------------------

def make_practice(rank, title, confidence="high", effort="M", year=2024, org="Org", kind="achieved"):
    return Practice(
        id=title, rank=rank, organization=org, title=title, what_they_did="x", result="y", result_kind=kind, result_year=year,
        prerequisites="p", effort=effort, expected_benefit="b", adaptation_for_dubai_police="a", risks="r",
        source_name="S", source_url="https://x", confidence=confidence, generated_at="t",
    )


def test_ranking_prefers_evidence_then_smaller_effort_then_recent_result():
    rows = [
        {"confidence": "low", "effort": "S", "result_year": 2025},
        {"confidence": "high", "effort": "L", "result_year": 2020},
        {"confidence": "high", "effort": "S", "result_year": 2021},
        {"confidence": "high", "effort": "S", "result_year": 2024},
        {"confidence": "high", "effort": None, "result_year": 2025},
    ]
    ordered = sorted(rows, key=rank_key)
    assert [(r["confidence"], r["effort"], r["result_year"]) for r in ordered] == [
        ("high", "S", 2024), ("high", "S", 2021), ("high", "L", 2020), ("high", None, 2025), ("low", "S", 2025),
    ]


def test_achieved_results_rank_before_estimates_of_the_same_confidence():
    rows = [
        {"confidence": "high", "result_kind": "estimated", "effort": "S", "result_year": 2026},
        {"confidence": "high", "result_kind": "achieved", "effort": "L", "result_year": 2020},
    ]
    assert [r["result_kind"] for r in sorted(rows, key=rank_key)] == ["achieved", "estimated"]


def test_the_recommendation_names_the_top_practice_and_matches_the_cards():
    cards = [make_practice(1, "Alpha", effort="S"), make_practice(2, "Beta", effort=None, kind="estimated")]
    text = build_recommendation(cards, "Gap")
    assert "start with Alpha (Org): high evidence, achieved result, effort S" in text
    assert "Beta (Org): high evidence, estimated result" in text and "1 practice(s) have no effort estimate" in text
    assert "not a decision" in text
    assert build_recommendation([], "Gap") is None


# ---- running ---------------------------------------------------------------------------

@sync
async def test_running_a_trend_gap_searches_once_and_saves_ranked_cards():
    service, brain = make()
    brain.search_queue.append(answer(
        practice(url=URL_A, title="Low", confidence="low"),
        practice(url=URL_B, title="Top", organization="RAND", confidence="high", effort="S"),
    ))

    r = await service.run(gap_id=DT)

    assert len(brain.searches) == 1
    assert [p.title for p in r.practices] == ["Top", "Low"] and [p.rank for p in r.practices] == [1, 2]
    assert r.status == "draft" and r.ai_generated is True and r.practices[0].handoff == "not_sent"
    assert "start with Top" in r.recommendation
    search = brain.searches[0]
    assert search["tool_choice"] == "required" and search["tools"][0]["filters"]["allowed_domains"]
    assert "Digital twins of security systems" in search["input"] and "Platform demo data" in search["input"]
    assert "https://www" not in search["input"]  # no real link is ever given to the model


@sync
async def test_saved_practices_are_reused_without_searching():
    service, brain = make()
    brain.search_queue.append(answer(practice()))
    await service.run(gap_id=DT)

    again = await service.run(gap_id=DT)

    assert len(brain.searches) == 1 and len(again.practices) == 1


@sync
async def test_refresh_searches_again_and_replaces_the_cards():
    service, brain = make()
    brain.search_queue += [answer(practice(title="Old")), answer(practice(title="New", url=URL_B))]
    await service.run(gap_id=DT)

    r = await service.run(gap_id=DT, refresh=True)

    assert [p.title for p in r.practices] == ["New"] and len(brain.searches) == 2


@sync
async def test_nothing_found_is_a_normal_answer():
    service, brain = make()
    brain.search_queue.append(answer())

    r = await service.run(gap_id=DT)

    assert r.practices == [] and r.recommendation is None
    assert "No practice with a published, measurable result" in r.warning


@sync
async def test_an_empty_search_never_wipes_earlier_practices():
    service, brain = make()
    brain.search_queue += [answer(practice()), answer()]
    await service.run(gap_id=DT)

    r = await service.run(gap_id=DT, refresh=True)

    assert len(r.practices) == 1 and "earlier results" in r.warning


@sync
async def test_an_unreadable_answer_is_retried_once_then_reported():
    service, brain = make()
    brain.search_queue += [FakeResponse("nope"), FakeResponse("still nope")]
    from app.core.errors import AppError

    with pytest.raises(AppError) as exc:
        await service.run(gap_id=DT)
    assert exc.value.code == "no_valid_results" and len(brain.searches) == 2

    service2, brain2 = make()
    brain2.search_queue += [FakeResponse("nope"), answer(practice())]
    assert len((await service2.run(gap_id=DT)).practices) == 1


@sync
async def test_repeated_results_are_removed_before_ranking_cards():
    service, brain = make()
    brain.search_queue.append(answer(
        practice(title="First", result="800 hours reviewed in 3 hours."),
        practice(title="Second", result="800 hours reviewed in 3 hours, one arrest.", confidence="medium"),
    ))
    r = await service.run(gap_id=DT)
    assert [p.title for p in r.practices] == ["First"]


@sync
async def test_the_card_limit_is_respected():
    service, brain = make(best_practices_max_cards=2)
    brain.search_queue.append(answer(*[practice(title=f"P{i}", url=URL_A + f"?{i}") for i in range(4)]))

    r = await service.run(gap_id=DT)

    assert len(r.practices) == 2


@sync
async def test_a_typed_gap_is_searched_and_remembered():
    service, brain = make()
    brain.search_queue.append(answer(practice()))

    r = await service.run(custom_gap="Speed up approval of public safety permits")

    assert r.gap.origin == "custom" and r.gap.id.startswith("custom-")
    assert "permits" in brain.searches[0]["input"] and "None available for this gap" in brain.searches[0]["input"]
    again = await service.run(custom_gap="speed up   approval of public safety permits")
    assert again.gap.id == r.gap.id and len(brain.searches) == 1
    assert r.gap.id in [g.id for g in service.gaps()]


@sync
async def test_unknown_or_missing_gaps_are_errors():
    from app.core.errors import AppError

    service, _ = make()
    with pytest.raises(AppError) as e1:
        await service.run(gap_id="nonsense")
    with pytest.raises(AppError) as e2:
        await service.run()
    with pytest.raises(AppError) as e3:
        await service.run(custom_gap="hi")
    assert (e1.value.status_code, e2.value.status_code, e3.value.status_code) == (404, 422, 422)


@sync
async def test_only_the_newest_typed_gaps_are_kept():
    service, brain = make(best_practices_max_custom_gaps=2)
    for minute, text in enumerate(("first typed gap here", "second typed gap here", "third typed gap here"), start=1):
        service._now = lambda m=minute: datetime(2026, 11, 18, 6, m, tzinfo=timezone.utc)
        brain.search_queue.append(answer(practice()))
        await service.run(custom_gap=text)
    titles = [g.title for g in service.gaps() if g.origin == "custom"]
    assert titles == ["third typed gap here", "second typed gap here"]


@sync
async def test_practices_become_outdated_when_agent_1_changes():
    horizon = FakeHorizon(**{DT: [card("First trend")]})
    service, brain = make(horizon=horizon)
    brain.search_queue.append(answer(practice()))
    r = await service.run(gap_id=DT)
    assert r.outdated is False

    horizon.per_topic[DT] = [card("A brand new trend")]

    assert service.get(DT).outdated is True
    assert "changed since this search" in service.get(DT).warning


def test_reading_before_any_search_says_so_and_does_not_search():
    service, brain = make()
    r = service.get(DT)
    assert r.practices == [] and "No search has been run" in r.warning and brain.searches == []


# ---- saving ------------------------------------------------------------------------------

@sync
async def test_results_survive_a_restart(tmp_path):
    store = JsonFileStore(tmp_path / "bp.json")
    service, brain = make(store=store)
    brain.search_queue.append(answer(practice()))
    await service.run(gap_id=DT)
    await service.run(custom_gap="a gap typed by a person")  # empty search, still remembered
    assert "a gap typed by a person" in str(store.load())

    again, brain2 = make(store=JsonFileStore(tmp_path / "bp.json"))
    r = again.get(DT)

    assert len(r.practices) == 1 and r.last_run and brain2.searches == []
    assert again.status()["gaps_saved"] == 2


def test_a_damaged_store_is_ignored(tmp_path):
    path = tmp_path / "bp.json"
    path.write_text(json.dumps({"gaps": {"digital-twin": {"junk": True}}}), encoding="utf-8")
    service, _ = make(store=JsonFileStore(path))
    assert service.get(DT).practices == []


# ---- the conversation ----------------------------------------------------------------------

def chat_req(message, history=None):
    from app.schemas.best_practices import PracticeChatRequest

    return PracticeChatRequest(message=message, history=history or [])


@sync
async def test_a_clear_request_runs_straight_away():
    service, brain = make()
    brain.plan_queue.append(plan_answer("run", plan={"gap_id": DT, "custom_gap": None, "refresh": False}))
    brain.search_queue.append(answer(practice()))

    r = await service.chat(chat_req("How do others use digital twins?"))

    assert r.type == "result" and len(r.result.practices) == 1 and r.plan.gap_id == DT
    assert "Digital Twin" in brain.plans[0]["instructions"] and "digital-twin:" in brain.plans[0]["instructions"]


@sync
async def test_a_vague_request_gets_one_question_with_choices():
    service, brain = make()
    brain.plan_queue.append(plan_answer("ask", "Which gap?", suggestions=["Digital Twin", "AI Policing"]))

    r = await service.chat(chat_req("show me best practices"))

    assert r.type == "question" and r.suggestions == ["Digital Twin", "AI Policing"] and brain.searches == []


@sync
async def test_after_enough_questions_it_runs_with_the_most_pressing_gap():
    from app.schemas.chat import ChatMessage

    service, brain = make(best_practices_max_questions=1)
    brain.plan_queue.append(plan_answer("ask", "Which gap?"))
    brain.search_queue.append(answer(practice()))
    history = [ChatMessage(role="user", content="best practices"), ChatMessage(role="assistant", content="Which gap?")]

    r = await service.chat(chat_req("anything", history))

    assert r.type == "result" and r.plan.gap_id == service.gaps()[0].id
    assert "most pressing gap" in r.message


@sync
async def test_a_typed_gap_in_the_conversation_is_searched():
    service, brain = make()
    brain.plan_queue.append(plan_answer("run", plan={"gap_id": None, "custom_gap": "Reduce emergency call response time", "refresh": False}))
    brain.search_queue.append(answer(practice()))

    r = await service.chat(chat_req("response times"))

    assert r.type == "result" and r.result.gap.origin == "custom" and "response time" in brain.searches[0]["input"]


@sync
async def test_an_off_topic_message_gets_a_plain_reply_and_no_search():
    service, brain = make()
    brain.plan_queue.append(plan_answer("message", "I find best practices for gaps."))

    r = await service.chat(chat_req("what is the weather"))

    assert r.type == "message" and brain.searches == []


@sync
async def test_an_unreadable_planner_answer_asks_again():
    service, brain = make()
    brain.plan_queue.append(FakeResponse("garbage"))

    r = await service.chat(chat_req("hello"))

    assert r.type == "question" and brain.searches == []


@sync
async def test_no_gaps_at_all_asks_for_a_typed_gap_without_calling_the_model():
    service, brain = make(horizon=FakeHorizon(), context={})

    r = await service.chat(chat_req("best practices"))

    assert r.type == "message" and "own words" in r.message and brain.plans == []


# ---- helpers ------------------------------------------------------------------------------------

def test_plan_helpers_are_safe():
    assert clean_plan({"gap_id": "DIGITAL-TWIN"}, [DT]) == {"gap_id": DT, "custom_gap": None, "refresh": False}
    assert clean_plan({"gap_id": "nope", "custom_gap": "a real typed gap", "refresh": True}, [DT])["custom_gap"] == "a real typed gap"
    assert clean_plan({"gap_id": "nope", "custom_gap": "hi"}, [DT]) == {"gap_id": None, "custom_gap": None, "refresh": False}
    assert parse_plan('{"action": "dance"}') is None and parse_plan(None) is None
    assert clean_custom_gap("   ") is None and custom_gap_id("A  b") == custom_gap_id("a b")


def test_the_domain_list_is_within_the_search_limit_and_has_no_links():
    assert 1 <= len(DOMAINS) <= 20
    assert all("/" not in d.domain and "http" not in d.domain for d in DOMAINS)


# ---- the API -------------------------------------------------------------------------------------

def test_api_gaps_run_read_and_chat():
    service, brain = make()
    brain.search_queue.append(answer(practice()))
    brain.plan_queue.append(plan_answer("ask", "Which gap?", suggestions=["Digital Twin"]))
    tc = client_for(service)

    gaps = tc.get("/agents/best-practices/gaps").json()["gaps"]
    assert any(g["id"] == DT for g in gaps)

    assert tc.get(f"/agents/best-practices?gap={DT}").json()["practices"] == []
    ran = tc.post("/agents/best-practices/run", json={"gap_id": DT})
    body = ran.json()
    assert ran.status_code == 200 and body["status"] == "draft" and len(body["practices"]) == 1
    assert body["disclaimer"] and body["recommendation"]
    assert len(tc.get(f"/agents/best-practices?gap={DT}").json()["practices"]) == 1

    chat = tc.post("/agents/best-practices/chat", json={"message": "best practices", "history": []})
    assert chat.status_code == 200 and chat.json()["type"] == "question"


def test_api_errors_are_clear():
    service, _ = make()
    tc = client_for(service)
    assert tc.get("/agents/best-practices?gap=nope").status_code == 404
    assert tc.post("/agents/best-practices/run", json={}).status_code == 422
    assert tc.get("/agents/best-practices").status_code == 422
