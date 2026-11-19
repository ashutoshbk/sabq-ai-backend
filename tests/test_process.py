"""Process & Procedure Analyst: quote checks, maturity maths, step counts, saving, the API."""

import asyncio
import functools
import json

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_process_service
from app.core.store import JsonFileStore
from app.main import create_app
from app.services.agent_common import load_resource
from app.services.process_parsing import parse_analysis
from app.services.process_service import ProcessService
from tests.conftest import FakeClient, FakeResponse, make_settings

RUBRIC = load_resource("maturity_rubric.json")["dimensions"]
SOP = (
    "Procedure for training requests. Owner: Training Section. "
    "1. The employee fills the form (1 day). 2. The manager approves (2 days). "
    "3. The department head approves again (2 days). 4. Training registers the employee."
)


def sync(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))

    return wrapper


def answer(**over):
    data = {
        "steps": [
            {"id": "S1", "action": "Employee fills the form", "owner": "Employee", "system": None, "est_time": "1 day"},
            {"id": "S2", "action": "Manager approves", "owner": "Manager", "system": "null", "est_time": "2 days"},
            {"id": "S3", "action": "Department head approves again", "owner": "Department head", "est_time": "2 days"},
            {"id": "S4", "action": "Training registers the employee", "owner": "Training", "est_time": None},
        ],
        "issues": [
            {"type": "duplicate_approval", "description": "Two approvals in a row", "step_ids": ["S2", "S3"],
             "evidence": "The department head approves again"},
        ],
        "maturity": [
            {"dimension": "documentation", "level": 3, "reason": "Written steps", "evidence": "Owner: Training Section"},
            {"dimension": "measurement", "level": 4, "reason": "Has KPIs", "evidence": "the whole process is measured weekly"},
        ],
        "proposed_flow": [
            {"id": "X", "action": "Employee fills the form", "replaces": ["S1"]},
            {"id": "Y", "action": "Manager approves", "replaces": ["S2", "S3", "S9"]},
            {"id": "Z", "action": "Training registers the employee", "replaces": ["S4"]},
        ],
        "needs_clarification": ["Who checks the budget?"],
        "expected_time_saving": "About 2 days",
    }
    data.update(over)
    return json.dumps(data)


def parse(text=None, sop=SOP):
    return parse_analysis(text if text is not None else answer(), sop, RUBRIC)


# ---- checking the answer ----------------------------------------------------------------------

def test_steps_get_clean_ids_and_blank_fields_become_none():
    steps = parse()["steps"]
    assert [s["id"] for s in steps] == ["S1", "S2", "S3", "S4"]
    assert steps[1]["system"] is None and steps[0]["system"] is None and steps[3]["est_time"] is None


def test_an_issue_is_kept_with_a_verified_quote():
    issue = parse()["issues"][0]
    assert issue["type"] == "duplicate_approval" and issue["step_ids"] == ["S2", "S3"] and issue["evidence_verified"] is True


def test_an_issue_pointing_at_nothing_is_dropped():
    text = answer(issues=[{"type": "rework_loop", "description": "Loop", "step_ids": ["S99"], "evidence": "invented quote that is not there"}])
    assert parse(text)["issues"] == []


def test_a_score_with_a_real_quote_is_kept_and_one_without_is_lowered_to_1():
    dims = {d["id"]: d for d in parse()["maturity"]["dimensions"]}
    assert dims["documentation"]["level"] == 3 and dims["documentation"]["evidence_verified"] is True
    assert dims["measurement"]["level"] == 1 and "Lowered to 1" in dims["measurement"]["reason"]
    assert dims["measurement"]["evidence"] is None  # a quote that is not in the SOP is not shown


def test_missing_dimensions_are_scored_1_and_the_overall_is_computed_in_code():
    m = parse()["maturity"]
    assert len(m["dimensions"]) == len(RUBRIC) and m["overall"] == round((3 + 1 + 1 + 1 + 1) / 5, 1)
    assert next(d for d in m["dimensions"] if d["id"] == "control")["reason"].startswith("Not assessed")


def test_unknown_dimensions_and_levels_outside_1_to_5_are_ignored():
    text = answer(maturity=[{"dimension": "magic", "level": 5, "evidence": SOP[:30]}, {"dimension": "ownership", "level": 9, "evidence": SOP[:30]}])
    assert all(d["level"] == 1 for d in parse(text)["maturity"]["dimensions"])


def test_step_counts_are_computed_and_unknown_replaced_ids_are_dropped():
    out = parse()
    assert out["steps_before"] == 4 and out["steps_after"] == 3
    assert out["proposed_flow"][1]["replaces"] == ["S2", "S3"]  # S9 does not exist
    assert [f["id"] for f in out["proposed_flow"]] == ["N1", "N2", "N3"]


def test_missing_owners_and_times_are_reported_not_assumed():
    text = answer(steps=[{"id": "1", "action": "Do a thing"}, {"id": "2", "action": "Do another"}])
    out = parse(text)
    assert "No step names an owner." in out["needs_clarification"]
    assert "No step states a time, so no time saving can be calculated." in out["needs_clarification"]
    assert out["expected_time_saving"].startswith("Cannot be estimated")


def test_unreadable_answers_are_none_and_a_missing_steps_list_too():
    assert parse("not json") is None
    assert parse(json.dumps({"issues": []})) is None


# ---- the service ---------------------------------------------------------------------------------

def make(store=None, **settings):
    fake = FakeClient()
    queue = []

    def handler(kwargs):
        calls.append(kwargs)
        return queue.pop(0) if queue else FakeResponse(answer())

    calls: list[dict] = []
    fake.responses.handler = handler
    service = ProcessService(fake, make_settings(**settings), store=store)
    return service, queue, calls


def client_for(service):
    app = create_app()
    app.dependency_overrides[get_process_service] = lambda: service
    return TestClient(app)


def test_the_process_list_has_the_platform_processes_and_the_sample():
    service, _, _ = make()
    rows = {p.code: p for p in service.processes()}
    assert {"OPS-001", "OPS-005", "SAMPLE-LEAVE"} <= set(rows)
    assert rows["OPS-001"].sop_available and rows["OPS-001"].demo_maturity == 5
    assert rows["OPS-002"].sop_available is False


@sync
async def test_analysing_a_demo_sop_calls_the_model_once_and_saves_the_result():
    service, queue, calls = make()
    queue.append(FakeResponse(answer()))

    r = await service.analyze(process_code="SAMPLE-LEAVE")

    assert len(calls) == 1 and "tools" not in calls[0]  # no web search
    assert r.status == "draft" and r.ai_generated and r.steps_before == 4
    assert "Training Section" in calls[0]["input"] or "external training" in calls[0]["input"]
    again = await service.analyze(process_code="sample-leave")
    assert len(calls) == 1 and again.last_run == r.last_run  # saved, no new call


@sync
async def test_refresh_calls_the_model_again():
    service, queue, calls = make()
    await service.analyze(process_code="OPS-001")
    await service.analyze(process_code="OPS-001", refresh=True)
    assert len(calls) == 2


@sync
async def test_the_platform_tile_value_is_shown_for_comparison_only():
    service, _, _ = make()
    r = await service.analyze(process_code="OPS-001")
    assert r.platform_maturity == 5 and r.maturity.overall is not None


@sync
async def test_pasted_text_is_analysed_and_reused_by_its_content():
    service, _, calls = make()
    first = await service.analyze(sop_text=SOP, title="My SOP")
    second = await service.analyze(sop_text=SOP)
    assert first.process.code.startswith("custom-") and first.process.code == second.process.code and len(calls) == 1
    assert first.process.name == "My SOP" and first.platform_maturity is None


@sync
async def test_missing_or_unknown_input_is_a_clear_error():
    from app.core.errors import AppError

    service, _, _ = make()
    with pytest.raises(AppError) as e1:
        await service.analyze(process_code="OPS-002")  # no SOP text for it
    with pytest.raises(AppError) as e2:
        await service.analyze()
    with pytest.raises(AppError) as e3:
        await service.analyze(sop_text="too short")
    assert [e.value.status_code for e in (e1, e2, e3)] == [422, 422, 422]
    with pytest.raises(AppError) as e5:
        await service.analyze(process_code="NOPE-999")
    assert e5.value.status_code == 404
    with pytest.raises(AppError) as e4:
        service.get("OPS-001")
    assert e4.value.status_code == 404


@sync
async def test_an_unreadable_answer_is_retried_once_then_reported():
    from app.core.errors import AppError

    service, queue, calls = make()
    queue += [FakeResponse("nope"), FakeResponse("nope again")]
    with pytest.raises(AppError) as exc:
        await service.analyze(process_code="OPS-001")
    assert exc.value.code == "no_valid_results" and len(calls) == 2


@sync
async def test_results_survive_a_restart(tmp_path):
    store = JsonFileStore(tmp_path / "p.json")
    service, _, _ = make(store=store)
    await service.analyze(process_code="OPS-001")

    again, _, calls = make(store=JsonFileStore(tmp_path / "p.json"))
    r = again.get("OPS-001")

    assert r.steps_before == 4 and calls == []
    assert [p for p in again.processes() if p.code == "OPS-001"][0].analyzed_at


def test_api_list_analyze_and_read():
    service, _, _ = make()
    tc = client_for(service)
    assert tc.get("/agents/process/analysis?process=OPS-001").status_code == 404
    assert len(tc.get("/agents/process/processes").json()) >= 6
    r = tc.post("/agents/process/analyze", json={"process_code": "SAMPLE-LEAVE"})
    assert r.status_code == 200 and r.json()["maturity"]["overall"] is not None and r.json()["disclaimer"]
    assert tc.get("/agents/process/analysis?process=SAMPLE-LEAVE").json()["steps_after"] == 3
    assert tc.post("/agents/process/analyze", json={}).status_code == 422
