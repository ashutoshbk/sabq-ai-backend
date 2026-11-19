"""Excellence Radar: evidence checks, readiness maths, countdown, saving, the API."""

import asyncio
import functools
import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_excellence_service
from app.core.store import JsonFileStore
from app.main import create_app
from app.services.agent_common import load_resource
from app.services.excellence_parsing import countdown, parse_assessment, parse_deadline, readiness_of, top_gaps
from app.services.excellence_service import ExcellenceService
from tests.conftest import FakeClient, FakeResponse, make_settings

CRITERIA = load_resource("excellence_criteria.json")["awards"]["efqm"]["criteria"]
EVIDENCE = load_resource("evidence_library.json")["evidence"]
NOW = datetime(2026, 11, 19, 6, 0, tzinfo=timezone.utc)


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))

    return wrapper


def answer(**over):
    data = {
        "criteria": [
            {"id": "E1", "matches": [{"evidence_id": "EV-01", "why": "Procedure"}, {"evidence_id": "ev-04", "why": "Report"}, {"evidence_id": "EV-99", "why": "invented"}], "coverage": 70, "missing": "Strategy cascade"},
            {"id": "E2", "matches": [{"evidence_id": "EV-99", "why": "invented"}], "coverage": 90, "missing": "Leadership proof"},
            {"id": "E3", "matches": [], "coverage": 40, "missing": "Stakeholder survey"},
            {"id": "e7", "matches": [{"evidence_id": "EV-07", "why": "Dashboard"}], "coverage": 150, "missing": ""},
        ],
        "owner_suggestions": [{"criterion_id": "E3", "owner_role": "Customer Happiness Department"}, {"criterion_id": "ZZ", "owner_role": "Ghost"}],
    }
    data.update(over)
    return json.dumps(data)


def parse(text=None):
    return parse_assessment(text if text is not None else answer(), CRITERIA, EVIDENCE)


# ---- checking the answer -----------------------------------------------------------------------------

def test_every_criterion_is_returned_in_order_even_if_the_model_skipped_some():
    out = parse()["criteria"]
    assert [c["id"] for c in out] == [c["id"] for c in CRITERIA]
    assert next(c for c in out if c["id"] == "E4")["coverage"] == 0


def test_only_evidence_that_exists_is_counted():
    e1 = parse()["criteria"][0]
    assert [m["evidence_id"] for m in e1["matches"]] == ["EV-01", "EV-04"]  # EV-99 does not exist, ev-04 is understood
    assert e1["matches"][0]["evidence_name"].startswith("Strategic Monitoring procedure")


def test_no_real_evidence_forces_coverage_to_zero_and_says_so():
    out = {c["id"]: c for c in parse()["criteria"]}
    assert out["E2"]["coverage"] == 0 and out["E2"]["no_evidence"] is True and out["E2"]["missing"].startswith("No evidence")
    assert out["E3"]["coverage"] == 0  # the model said 40 but listed nothing


def test_coverage_outside_0_to_100_is_ignored_and_ids_are_case_insensitive():
    e7 = next(c for c in parse()["criteria"] if c["id"] == "E7")
    assert e7["coverage"] == 0 and len(e7["matches"]) == 1  # 150 is not a valid percentage


def test_owner_suggestions_only_for_real_criteria():
    assert parse()["owners"] == {"E3": "Customer Happiness Department"}


def test_unreadable_answers_are_none():
    assert parse("nope") is None and parse(json.dumps({"x": 1})) is None


def test_readiness_is_the_average_computed_in_code():
    assert readiness_of([{"coverage": 70}, {"coverage": 0}, {"coverage": 30}]) == 33.3
    assert readiness_of([]) == 0.0


def test_top_gaps_are_the_weakest_first_and_at_most_five():
    results = [{"id": f"E{i}", "name": f"N{i}", "coverage": c, "missing": "m"} for i, c in enumerate([50, 0, 100, 20, 80, 10, 60], start=1)]
    gaps = top_gaps(results, {"E2": "Role A"}, "2027-03-01")
    assert [g["criterion_id"] for g in gaps] == ["E2", "E6", "E4", "E1", "E7"]
    assert gaps[0]["suggested_owner"] == "Role A" and gaps[0]["due"] == "2027-03-01" and all(g["coverage"] < 100 for g in gaps)


# ---- the countdown --------------------------------------------------------------------------------------

def test_countdown_tiers_and_checkpoints():
    today = date(2026, 11, 19)
    assert countdown(date(2027, 3, 31), today, True)["tier"] is None  # 132 days
    assert countdown(date(2027, 2, 1), today, False)["tier"] == "90"  # 74 days
    assert countdown(date(2026, 12, 31), today, False)["tier"] == "60"  # 42 days
    assert countdown(date(2026, 12, 10), today, False)["tier"] == "30"  # 21 days
    assert countdown(date(2026, 11, 1), today, False)["tier"] == "overdue"
    c = countdown(date(2027, 2, 17), today, False)  # exactly 90 days
    assert [(x["days_before"], x["state"]) for x in c["checkpoints"]] == [(90, "due_now"), (60, "upcoming"), (30, "upcoming")]
    assert c["days_left"] == 90 and c["checkpoints"][0]["date"] == "2026-11-19"


def test_deadline_parsing():
    assert parse_deadline("2027-03-31") == date(2027, 3, 31) and parse_deadline("31/03/2027") is None and parse_deadline(None) is None


# ---- the service ---------------------------------------------------------------------------------------

def make(store=None):
    fake = FakeClient()
    calls: list[dict] = []
    queue: list = []

    def handler(kwargs):
        calls.append(kwargs)
        return queue.pop(0) if queue else FakeResponse(answer())

    fake.responses.handler = handler
    return ExcellenceService(fake, make_settings(), store=store, now_fn=lambda: NOW), queue, calls


def client_for(service):
    app = create_app()
    app.dependency_overrides[get_excellence_service] = lambda: service
    return TestClient(app)


def test_the_award_list_shows_countdown_and_whether_criteria_exist():
    service, _, _ = make()
    rows = {a.id: a for a in service.awards()}
    assert len(rows) == 7 and rows["efqm"].criteria_available and rows["iso-9001"].criteria_available
    assert rows["skea"].criteria_available is False and rows["skea"].countdown is None
    assert rows["efqm"].countdown.deadline_is_demo is True and rows["efqm"].countdown.days_left == 132


@sync
async def test_assessing_calls_the_model_once_and_computes_everything_in_code():
    service, _, calls = make()

    r = await service.assess("efqm")

    assert len(calls) == 1 and "tools" not in calls[0]
    assert "EV-01" in calls[0]["input"] and "E1:" in calls[0]["input"]
    assert r.status == "draft" and r.ai_generated and len(r.criteria) == 7
    assert r.readiness == round((70 + 0 + 0 + 0 + 0 + 0 + 0) / 7, 1)  # E1 = 70, E7 forced to 0 (150 invalid)
    assert r.top_gaps[0].coverage == 0 and len(r.top_gaps) == 5
    assert r.countdown.deadline == "2027-03-31" and "demo deadline" in r.timeline_note
    assert r.top_gaps[0].due == "2027-03-01"


@sync
async def test_a_saved_assessment_is_reused_and_refresh_calls_again():
    service, _, calls = make()
    await service.assess("efqm")
    await service.assess("EFQM")
    assert len(calls) == 1
    await service.assess("efqm", refresh=True)
    assert len(calls) == 2


@sync
async def test_a_new_deadline_only_changes_the_countdown_not_the_model_call():
    service, _, calls = make()
    await service.assess("efqm")

    r = await service.assess("efqm", deadline="2026-12-15")

    assert len(calls) == 1 and r.countdown.days_left == 26 and r.countdown.tier == "30" and r.countdown.deadline_is_demo is False
    assert "you sent" in r.timeline_note and r.top_gaps[0].due == "2026-11-15"


@sync
async def test_an_award_without_a_deadline_says_so():
    service, _, _ = make()
    r = await service.assess("iso-9001")
    assert r.countdown is None and "No deadline is known" in r.timeline_note
    with_deadline = await service.assess("iso-9001", deadline="2027-06-01")
    assert with_deadline.countdown.days_left == 194


@sync
async def test_errors_are_clear():
    from app.core.errors import AppError

    service, _, _ = make()
    with pytest.raises(AppError) as e1:
        await service.assess("nonsense")
    with pytest.raises(AppError) as e2:
        await service.assess("skea")  # no criteria loaded
    with pytest.raises(AppError) as e3:
        await service.assess("efqm", deadline="soon")
    with pytest.raises(AppError) as e4:
        service.get("efqm")
    assert [e.value.status_code for e in (e1, e2, e3, e4)] == [404, 422, 422, 404]
    assert "criteria_missing" == e2.value.code


@sync
async def test_an_unreadable_answer_is_retried_once_then_reported():
    from app.core.errors import AppError

    service, queue, calls = make()
    queue += [FakeResponse("nope"), FakeResponse("no")]
    with pytest.raises(AppError):
        await service.assess("efqm")
    assert len(calls) == 2


@sync
async def test_results_survive_a_restart(tmp_path):
    service, _, _ = make(store=JsonFileStore(tmp_path / "e.json"))
    await service.assess("efqm")

    again, _, calls = make(store=JsonFileStore(tmp_path / "e.json"))

    assert again.get("efqm").readiness == 10.0 and calls == []
    assert {a.id: a for a in again.awards()}["efqm"].readiness == 10.0


def test_api_list_assess_and_read():
    service, _, _ = make()
    tc = client_for(service)
    assert tc.get("/agents/excellence/assessment?award=efqm").status_code == 404
    assert len(tc.get("/agents/excellence/awards").json()) == 7
    r = tc.post("/agents/excellence/assess", json={"award_id": "efqm"})
    assert r.status_code == 200 and r.json()["disclaimer"] and r.json()["readiness"] == 10.0
    assert tc.get("/agents/excellence/assessment?award=efqm").json()["criteria_set"].startswith("EFQM")
    assert tc.post("/agents/excellence/assess", json={"award_id": "skea"}).status_code == 422
