"""Improvement Portfolio: checks, priority score and recommendation in code, opportunities, saving, the API."""

import asyncio
import functools
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_portfolio_service
from app.core.resources import load_topics
from app.core.store import JsonFileStore
from app.main import create_app
from app.services.agent_common import load_platform, load_resource
from app.services.portfolio_parsing import parse_evaluation, priority_score, rationale, recommend
from app.services.portfolio_service import PortfolioService
from tests.conftest import FakeClient, FakeResponse, make_settings

PLATFORM = load_platform()
RULES = load_resource("portfolio_rules.json")
CODES = {i["code"]: i["title"] for i in PLATFORM["portfolio"]}
OBJ = {o["id"] for o in PLATFORM["objectives"]}
NOW = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))

    return wrapper


def scores(alignment=4, impact=4, effort=2, risk=2, urgency=3):
    return {c: {"score": v, "reason": f"{c} reason"} for c, v in
            dict(alignment=alignment, impact=impact, effort=effort, risk=risk, urgency=urgency).items()}


def answer(sc=None, **over):
    data = {
        "overlaps": [{"code": "prt-001", "reason": "Same planning digital scope"}, {"code": "PRT-999", "reason": "invented"}],
        "scores": sc or scores(),
        "objective_ids": ["obj-3", "OBJ-9", "OBJ-3"],
        "charter": {
            "title": "AI call triage pilot", "problem": "Desk work takes officer time", "objective": "Free officer time",
            "scope": "Non-emergency calls", "kpis": [{"name": "Calls triaged automatically", "baseline": "", "target": "Up"}, {"name": ""}],
            "milestones": [{"title": "Pilot live", "month": 6}, {"title": "Design", "month": 2}, {"title": "Bad", "month": 99}],
            "owner_role": "", "dependencies": ["Call data access", ""],
        },
    }
    data.update(over)
    return json.dumps(data)


def parse(text=None):
    return parse_evaluation(text if text is not None else answer(), CODES, OBJ)


# ---- checking the answer -----------------------------------------------------------------------------

def test_overlaps_must_be_real_portfolio_items_and_objectives_must_exist():
    out = parse()
    assert [o["code"] for o in out["overlaps"]] == ["PRT-001"] and out["overlaps"][0]["title"] == CODES["PRT-001"]
    assert out["objectives"] == ["OBJ-3"]


def test_a_missing_or_invalid_score_is_never_assumed():
    for bad in (None, 0, 6, "high"):
        sc = scores()
        sc["impact"] = {"score": bad, "reason": "x"}
        assert parse(answer(sc)) is None
    sc = scores()
    del sc["urgency"]
    assert parse(answer(sc)) is None


def test_charter_defaults_are_honest():
    c = parse()["charter"]
    assert c["kpis"] == [{"name": "Calls triaged automatically", "baseline": "to be measured", "target": "Up"}]
    assert [m["month"] for m in c["milestones"]] == [2, 6]  # sorted, the month-99 one dropped
    assert c["owner_role"] == "To be assigned" and c["dependencies"] == ["Call data access"]


def test_a_charter_without_title_problem_or_objective_is_unreadable():
    bad = json.loads(answer())
    bad["charter"]["problem"] = ""
    assert parse(json.dumps(bad)) is None


def test_unreadable_answers_are_none():
    assert parse("nope") is None and parse(json.dumps({"scores": {}})) is None


# ---- the maths and the rule ----------------------------------------------------------------------------

def test_priority_is_a_weighted_average_where_effort_and_risk_count_in_reverse():
    w = RULES["weights"]
    # alignment 5, impact 5, urgency 5, effort 1, risk 1 -> every term is 5 -> 5.0
    assert priority_score(scores(5, 5, 1, 1, 5), w) == 5.0
    assert priority_score(scores(1, 1, 5, 5, 1), w) == 1.0
    # 0.30*4 + 0.25*4 + 0.15*3 + 0.15*(6-2) + 0.15*(6-2) = 1.2 + 1.0 + 0.45 + 0.6 + 0.6 = 3.85
    assert priority_score(scores(4, 4, 2, 2, 3), w) == 3.85


def test_the_recommendation_follows_the_thresholds_and_overlap_wins():
    t = RULES["thresholds"]
    status = {"PRT-001": "approved", "PRT-009": "rejected"}
    assert recommend(3.85, [], status, t) == ("Adopt", None)
    assert recommend(3.0, [], status, t) == ("Defer", None)
    assert recommend(2.0, [], status, t) == ("Reject", None)
    assert recommend(4.9, [{"code": "PRT-001"}], status, t) == ("Merge", "PRT-001")
    assert recommend(4.9, [{"code": "PRT-009"}], status, t) == ("Adopt", None)  # a rejected initiative is no reason to merge


def test_the_rationale_matches_the_numbers():
    t = RULES["thresholds"]
    text = rationale(3.85, "Adopt", None, scores(4, 4, 2, 2, 3), t)
    assert "3.85 of 5" in text and "Strongest: alignment (4)" in text and "Weakest: urgency (3)" in text
    assert "lower score is better" not in text and "Recommendation: Adopt" in text
    low_effort = rationale(3.5, "Defer", None, scores(3, 3, 1, 3, 3), t)  # effort 1 is the best possible effort
    assert "Strongest: effort (1)" in low_effort and "lower score is better" in low_effort
    assert "merge it there" in rationale(4.0, "Merge", "PRT-001", scores(), t)


# ---- the service -----------------------------------------------------------------------------------------

def make(store=None, best_practices=None, horizon=None):
    fake = FakeClient()
    calls: list[dict] = []
    queue: list = []

    def handler(kwargs):
        calls.append(kwargs)
        return queue.pop(0) if queue else FakeResponse(answer())

    fake.responses.handler = handler
    service = PortfolioService(fake, make_settings(), store=store, best_practices=best_practices, horizon=horizon,
                               topics=load_topics(), now_fn=lambda: NOW)
    return service, queue, calls


def client_for(service):
    app = create_app()
    app.dependency_overrides[get_portfolio_service] = lambda: service
    return TestClient(app)


class FakeBestPractices:
    def gaps(self):
        return [SimpleNamespace(id="digital-twin", practices_count=1), SimpleNamespace(id="x", practices_count=0)]

    def get(self, gap_id):
        p = SimpleNamespace(id="p1", title="National AI centre", organization="Home Office",
                            what_they_did="Created a centre.", result="800 hours in 3.", adaptation_for_dubai_police="Needs a hub.")
        return SimpleNamespace(practices=[p])


class FakeHorizon:
    def get_cards(self, topic, top_n=None, rank_by="relevance"):
        card = SimpleNamespace(trend="Digital twins for operations", summary="Cities simulate.", so_what_for_dubai_police="Plan pilots.")
        return SimpleNamespace(topics=[SimpleNamespace(cards=[card] if topic == "digital-twin" else [])])


def test_the_existing_portfolio_is_listed():
    service, _, _ = make()
    items = service.items()
    assert len(items) == 5 and items[0].code == "PRT-001" and items[0].status == "approved"


def test_opportunities_come_from_agent_3_practices_and_agent_1_cards():
    service, _, _ = make(best_practices=FakeBestPractices(), horizon=FakeHorizon())
    opps = {o.id: o for o in service.opportunities()}
    assert set(opps) == {"practice:digital-twin:p1", "trend:digital-twin"}
    assert opps["practice:digital-twin:p1"].source == "Best Practices" and "Needs a hub" in opps["practice:digital-twin:p1"].description
    assert opps["trend:digital-twin"].source == "Horizon Scan" and "Plan pilots" in opps["trend:digital-twin"].description


def test_a_failing_agent_does_not_break_the_opportunity_list():
    class Broken:
        def gaps(self):
            raise RuntimeError("down")

        def get_cards(self, *a, **k):
            raise RuntimeError("down")

    service, _, _ = make(best_practices=Broken(), horizon=Broken())
    assert service.opportunities() == []


@sync
async def test_evaluating_a_typed_opportunity_computes_score_and_recommendation_in_code():
    service, _, calls = make()

    r = await service.evaluate(title="AI call triage", description="Use AI to transcribe and triage non-emergency calls to free officer time.")

    assert len(calls) == 1 and "tools" not in calls[0] and "PRT-001" in calls[0]["input"] and "OBJ-1" in calls[0]["input"]
    assert r.opportunity.source == "Custom" and r.priority_score == 3.85
    assert r.recommendation == "Merge" and r.merge_with == "PRT-001"  # the model flagged an overlap with a live initiative
    assert [s.criterion for s in r.scores] == ["alignment", "impact", "effort", "risk", "urgency"] and r.scores[0].weight == 0.30
    assert r.decision == "pending" and r.status == "draft" and "3.85" in r.rationale


@sync
async def test_without_overlap_the_threshold_decides():
    service, queue, _ = make()
    queue.append(FakeResponse(answer(overlaps=[])))
    r = await service.evaluate(title="Something new here", description="A description with enough words to be accepted.")
    assert r.recommendation == "Adopt" and r.merge_with is None


@sync
async def test_a_saved_evaluation_is_reused_and_refresh_calls_again():
    service, _, calls = make(best_practices=FakeBestPractices(), horizon=FakeHorizon())
    await service.evaluate(opportunity_id="trend:digital-twin")
    await service.evaluate(opportunity_id="trend:digital-twin")
    assert len(calls) == 1
    await service.evaluate(opportunity_id="trend:digital-twin", refresh=True)
    assert len(calls) == 2
    assert {o.id: o.evaluated for o in service.opportunities()}["trend:digital-twin"] is True


@sync
async def test_errors_are_clear_and_unreadable_answers_are_retried_once():
    from app.core.errors import AppError

    service, queue, calls = make()
    with pytest.raises(AppError) as e1:
        await service.evaluate()
    with pytest.raises(AppError) as e2:
        await service.evaluate(opportunity_id="trend:nothing")
    with pytest.raises(AppError) as e3:
        service.get("custom-1")
    assert [e.value.status_code for e in (e1, e2, e3)] == [422, 404, 404]
    queue += [FakeResponse("nope"), FakeResponse("nope")]
    with pytest.raises(AppError):
        await service.evaluate(title="Another opportunity", description="Long enough description for the check.")
    assert len(calls) == 2


@sync
async def test_results_survive_a_restart(tmp_path):
    service, _, _ = make(store=JsonFileStore(tmp_path / "p.json"))
    first = await service.evaluate(title="AI call triage", description="Use AI to transcribe and triage calls for officers.")
    again, _, calls = make(store=JsonFileStore(tmp_path / "p.json"))
    assert again.get(first.opportunity.id).priority_score == 3.85 and calls == []


def test_api_flow():
    service, _, _ = make(best_practices=FakeBestPractices(), horizon=FakeHorizon())
    tc = client_for(service)
    assert len(tc.get("/agents/portfolio/items").json()) == 5
    opps = tc.get("/agents/portfolio/opportunities").json()["opportunities"]
    assert len(opps) == 2
    r = tc.post("/agents/portfolio/evaluate", json={"opportunity_id": opps[0]["id"]})
    assert r.status_code == 200 and r.json()["recommendation"] in ("Adopt", "Defer", "Merge", "Reject") and r.json()["charter"]["milestones"]
    assert tc.get(f"/agents/portfolio/evaluation?opportunity={opps[0]['id']}").status_code == 200
    assert tc.post("/agents/portfolio/evaluate", json={}).status_code == 422
