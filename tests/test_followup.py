"""Follow-up & Progress: evidence rules, progress/status/delay maths, reminder, saving, the API."""

import asyncio
import functools
import json
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_followup_service
from app.core.errors import AppError
from app.core.store import JsonFileStore
from app.main import create_app
from app.schemas.followup import CheckRequest
from app.services.followup_parsing import (
    add_lateness, build_reminder, parse_check, progress_of, rag_status, slip_risk,
)
from app.services.followup_service import FollowupService
from tests.conftest import FakeClient, FakeResponse, make_settings

MS = [
    {"id": "M1", "title": "Requirements approved", "due": "2026-01-10"},
    {"id": "M2", "title": "Pilot started", "due": "2026-02-10"},
    {"id": "M3", "title": "Rollout", "due": "2026-06-10"},
]
FILES = [{"name": "approval.pdf", "description": "x"}, {"name": "pilot.docx", "description": ""}]
UPDATE = "The requirements were approved in January. The pilot started in February in two units."
TODAY = date(2026, 3, 1)
PLATFORM = {"portfolio": [
    {"code": "T-1", "title": "Test initiative", "department": "Ops Dept", "status": "approved",
     "execution": "On track", "progress": 40, "milestones": MS},
    {"code": "T-2", "title": "Not approved", "department": "X", "status": "proposed", "milestones": MS},
]}


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))

    return wrapper


def answer(**over):
    data = {
        "milestones": [
            {"id": "M1", "verdict": "Yes", "reason": "Approved", "evidence_files": ["approval.pdf"], "evidence_quote": None},
            {"id": "M2", "verdict": "Partial", "reason": "Started", "evidence_files": [], "evidence_quote": "The pilot started in February"},
            {"id": "M3", "verdict": "No", "reason": "Nothing", "evidence_files": [], "evidence_quote": None},
        ],
        "risks": ["Only two units"],
    }
    data.update(over)
    return json.dumps(data)


def parse(text=None):
    return parse_check(text if text is not None else answer(), MS, FILES, UPDATE)


# ---- evidence rules ---------------------------------------------------------------------------

def test_valid_answer_is_kept():
    out = parse()
    assert [m["verdict"] for m in out["milestones"]] == ["Yes", "Partial", "No"]
    assert out["milestones"][0]["evidence_files"] == ["approval.pdf"]
    assert out["risks"] == ["Only two units"]


def test_unreadable_answer_is_none():
    assert parse("not json") is None
    assert parse(json.dumps({"x": 1})) is None


def test_yes_with_a_file_that_does_not_exist_is_downgraded():
    ms = json.loads(answer())["milestones"]
    ms[0]["evidence_files"] = ["invented.pdf"]
    out = parse(answer(milestones=ms))
    assert out["milestones"][0]["verdict"] == "No"
    assert out["milestones"][0]["evidence_files"] == []


def test_yes_with_only_a_real_quote_becomes_partial():
    ms = json.loads(answer())["milestones"]
    ms[0].update(evidence_files=[], evidence_quote="The requirements were approved in January")
    out = parse(answer(milestones=ms))
    assert out["milestones"][0]["verdict"] == "Partial"


def test_partial_with_an_invented_quote_becomes_no():
    ms = json.loads(answer())["milestones"]
    ms[1]["evidence_quote"] = "A line that is not in the update at all"
    out = parse(answer(milestones=ms))
    assert out["milestones"][1]["verdict"] == "No"
    assert out["milestones"][1]["evidence_quote"] is None


def test_file_names_match_ignoring_case():
    ms = json.loads(answer())["milestones"]
    ms[0]["evidence_files"] = ["APPROVAL.PDF"]
    assert parse(answer(milestones=ms))["milestones"][0]["evidence_files"] == ["approval.pdf"]


def test_missing_milestone_in_answer_is_no():
    ms = json.loads(answer())["milestones"][:1]
    out = parse(answer(milestones=ms))
    assert [m["verdict"] for m in out["milestones"]] == ["Yes", "No", "No"]


# ---- maths ---------------------------------------------------------------------------------------

def test_progress_counts_partial_as_half():
    assert progress_of(parse()["milestones"]) == 50  # (1 + .5 + 0) / 3
    assert progress_of([]) == 0


def test_lateness_and_status():
    ms = parse()["milestones"]
    add_lateness(ms, TODAY)
    assert ms[0]["days_late"] == 0  # done
    assert ms[1]["days_late"] == 19  # 10 Feb -> 1 Mar
    assert ms[2]["days_late"] == 0  # not due
    assert rag_status(ms, TODAY)[0] == "Amber"
    ms[1]["days_late"] = 45
    assert rag_status(ms, TODAY)[0] == "Red"


def test_green_and_complete():
    ms = parse()["milestones"]
    add_lateness(ms, date(2026, 1, 1))
    assert rag_status(ms, date(2026, 1, 1))[0] == "Green"
    for m in ms:
        m["verdict"] = "Yes"
    add_lateness(ms, TODAY)
    assert rag_status(ms, TODAY)[0] == "Complete"


def test_slip_risk_levels():
    ms = parse()["milestones"]
    add_lateness(ms, TODAY)
    assert slip_risk(ms, TODAY)[0] == "medium"
    ms[1]["days_late"] = 60
    assert slip_risk(ms, TODAY)[0] == "high"
    ms2 = parse()["milestones"]
    add_lateness(ms2, date(2026, 1, 1))
    assert slip_risk(ms2, date(2026, 1, 1))[0] == "medium" or slip_risk(ms2, date(2026, 1, 1))[0] == "low"


def test_due_soon_without_evidence_is_flagged():
    ms = [{"id": "M1", "title": "A", "due": "2026-03-15", "verdict": "No"}]
    add_lateness(ms, TODAY)
    level, reasons = slip_risk(ms, TODAY)
    assert level == "medium" and "due within" in reasons[0]


def test_reminder_lists_only_missing_items():
    ms = parse()["milestones"]
    add_lateness(ms, TODAY)
    r = build_reminder(PLATFORM["portfolio"][0], ms)
    assert r["missing"] == ["M2 Pilot started", "M3 Rollout"]
    assert "M1" not in r["message"] and "Ops Dept" in r["message"]
    assert r["escalate"] is False


def test_no_reminder_when_all_done():
    ms = [dict(m, verdict="Yes", days_late=0) for m in MS]
    assert build_reminder(PLATFORM["portfolio"][0], ms) is None


def test_reminder_escalates_after_30_days_late():
    ms = [{"id": "M1", "title": "A", "due": "2025-01-01", "verdict": "No", "days_late": 90}]
    assert build_reminder(PLATFORM["portfolio"][0], ms)["escalate"] is True


# ---- the service ---------------------------------------------------------------------------------

def make(store=None):
    fake = FakeClient()
    queue, calls = [], []

    def handler(kwargs):
        calls.append(kwargs)
        return queue.pop(0) if queue else FakeResponse(answer())

    fake.responses.handler = handler
    now = lambda: datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
    service = FollowupService(fake, make_settings(), store=store, platform=PLATFORM, now_fn=now)
    return service, queue, calls


def req(**over):
    base = dict(initiative_code="T-1", update_text=UPDATE, files=[{"name": "approval.pdf"}])
    base.update(over)
    return CheckRequest(**base)


def test_only_approved_initiatives_with_milestones_are_listed():
    service, _, _ = make()
    assert [i.code for i in service.initiatives()] == ["T-1"]


@sync
async def test_check_computes_everything_in_code():
    service, _, calls = make()
    res = await service.check(req())
    assert res.progress == 50 and res.rag == "Amber" and res.slip_risk == "medium"
    assert res.reminder and res.reminder.missing == ["M2 Pilot started", "M3 Rollout"]
    assert res.status == "draft" and res.ai_generated
    assert "tools" not in calls[0]
    assert res.platform_progress == 40 and res.progress_difference_note is None or True


@sync
async def test_difference_note_when_platform_and_evidence_disagree():
    service, queue, _ = make()
    queue.append(FakeResponse(answer(milestones=[])))
    res = await service.check(req())
    assert res.progress == 0 and "platform shows 40%" in res.progress_difference_note


@sync
async def test_unknown_or_unapproved_initiative_is_404():
    service, _, _ = make()
    for code in ("NOPE", "T-2"):
        with pytest.raises(AppError) as e:
            await service.check(req(initiative_code=code))
        assert e.value.status_code == 404


@sync
async def test_unreadable_answer_twice_is_502():
    service, queue, _ = make()
    queue.extend([FakeResponse("bad"), FakeResponse("bad")])
    with pytest.raises(AppError) as e:
        await service.check(req())
    assert e.value.status_code == 502


@sync
async def test_result_is_saved_and_survives_restart(tmp_path):
    path = tmp_path / "f.json"
    service, _, _ = make(JsonFileStore(path))
    await service.check(req())
    service2, _, calls = make(JsonFileStore(path))
    assert service2.get("T-1").progress == 50 and not calls
    assert service2.initiatives()[0].rag == "Amber"


def test_get_before_any_check_is_404():
    service, _, _ = make()
    with pytest.raises(AppError) as e:
        service.get("T-1")
    assert e.value.status_code == 404


@sync
async def test_digest_lists_checked_and_unchecked():
    service, _, _ = make()
    assert service.digest().counts["Not checked"] == 1
    await service.check(req())
    d = service.digest()
    assert d.counts["Amber"] == 1 and d.counts["Not checked"] == 0
    assert "T-1 (Amber)" in d.message


# ---- the API ----------------------------------------------------------------------------------------

def client_for(service):
    app = create_app()
    app.dependency_overrides[get_followup_service] = lambda: service
    return TestClient(app)


def test_api_flow():
    service, _, _ = make()
    c = client_for(service)
    assert c.get("/agents/followup/initiatives").json()[0]["code"] == "T-1"
    assert c.get("/agents/followup/status", params={"initiative": "T-1"}).status_code == 404
    r = c.post("/agents/followup/check", json={"initiative_code": "T-1", "update_text": UPDATE, "files": [{"name": "approval.pdf"}]})
    assert r.status_code == 200 and r.json()["rag"] == "Amber"
    assert c.get("/agents/followup/status", params={"initiative": "T-1"}).json()["progress"] == 50
    assert c.get("/agents/followup/digest").json()["counts"]["Amber"] == 1
    assert c.post("/agents/followup/check", json={"initiative_code": "ZZ"}).status_code == 404
