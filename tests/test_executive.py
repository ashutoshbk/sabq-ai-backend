"""Executive Summary & Data Quality: checks, freshness, brief limits, sources, the API."""

from datetime import datetime, timezone
from types import SimpleNamespace as NS

from fastapi.testclient import TestClient

from app.api.deps import get_executive_service
from app.main import create_app
from app.services.executive_service import ExecutiveService
from tests.conftest import make_settings

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
PLATFORM = {
    "meta": {"last_update": "2025-05", "charts_without_labels": ["Bar chart"]},
    "portfolio": [{"code": "P-1", "title": "Slow one", "execution": "Delayed", "progress": 30},
                  {"code": "P-2", "title": "Fine one", "execution": "On track", "progress": 80}],
}


class Saved:
    def __init__(self, items):
        self.items = items

    def saved_items(self):
        return dict(self.items)


def card(title="Digital twins for cities", sources=1):
    return NS(trend=title, sources=[object()] * sources)


def horizon(cards, last_run="2026-02-20T00:00:00+00:00"):
    topic = NS(name="Digital Twin", last_run=last_run, cards=cards)
    return NS(get_cards=lambda: NS(topics=[topic]))


def bench(last_run="2026-02-25T00:00:00+00:00"):
    res = NS(last_run=last_run, indicators=[1, 2], insight="Dubai ranks 2nd of 5 on safety.",
             data_checks=[NS(code="duplicate_rank", severity="warning", message="Two entities share rank 3.")])
    return NS(get=lambda: res)


def bp(practices=2, last_run="2026-02-22T00:00:00+00:00"):
    gap = NS(title="Digital twin gap", practices_count=practices, last_run=last_run if practices else None)
    return NS(gaps=lambda: [gap])


def make(**kw):
    kw.setdefault("platform", PLATFORM)
    return ExecutiveService(make_settings(), now_fn=lambda: NOW, **kw)


def codes(report):
    return {c.code for c in report.checks}


def test_platform_checks():
    r = make().data_quality()
    assert {"platform_data_stale", "chart_without_labels", "initiative_delayed"} <= codes(r)
    assert next(c for c in r.checks if c.code == "platform_data_stale").message.count("10 months")


def test_fresh_platform_data_has_no_stale_warning():
    p = dict(PLATFORM, meta={"last_update": "2026-02"})
    assert "platform_data_stale" not in codes(make(platform=p).data_quality())


def test_unreadable_platform_date_is_reported_not_crashing():
    p = dict(PLATFORM, meta={"last_update": "soon"})
    assert "platform_date_unreadable" in codes(make(platform=p).data_quality())


def test_benchmark_checks_are_passed_on():
    r = make(benchmark=bench()).data_quality()
    assert "benchmark_duplicate_rank" in codes(r)


def test_modules_not_run_stale_and_fresh():
    old = {"T": {"last_run": "2025-01-01T00:00:00+00:00"}}
    r = make(horizon=horizon([card()]), excellence=Saved(old), followup=Saved({})).data_quality()
    states = {m.module: m.state for m in r.modules}
    assert states["Agent 1 Horizon Scanning"] == "fresh"
    assert states["Agent 5 Excellence Radar"] == "stale"
    assert states["Agent 7 Follow-up"] == "not run"
    assert {"results_stale", "module_not_run"} <= codes(r)


def test_cards_without_sources_flagged():
    assert "cards_without_sources" in codes(make(horizon=horizon([card(sources=0)])).data_quality())


def test_a_broken_module_does_not_break_the_report():
    def boom():
        raise RuntimeError("x")
    broken = NS(get_cards=boom, get=boom, gaps=boom, saved_items=boom)
    r = make(horizon=broken, benchmark=broken, best_practices=broken, process=broken).data_quality()
    assert r.checks


def full():
    return make(
        horizon=horizon([card()]), benchmark=bench(), best_practices=bp(),
        excellence=Saved({"efqm": {"award": {"title": "EFQM"}, "readiness": 42.5, "last_run": "2026-02-26T00:00:00+00:00",
                                    "countdown": {"days_left": 40, "deadline": "2026-04-10", "deadline_is_demo": True},
                                    "top_gaps": [{"criterion": "Results", "coverage": 10, "task": "Collect KPI data."}]}}),
        followup=Saved({"P-1": {"rag": "Red", "rag_reason": "Late by 90 days.", "last_run": "2026-02-27T00:00:00+00:00",
                                "initiative": {"code": "P-1", "title": "Slow one"}}}),
        portfolio=Saved({"x": {"decision": "pending", "priority_score": 4.1, "recommendation": "Adopt", "last_run": "2026-02-28T00:00:00+00:00",
                               "opportunity": {"title": "Drone dispatch"}}}),
        process=Saved({"OPS-001": {"process": {"name": "Strategic Monitoring"}, "maturity": {"overall": 3.4},
                                   "steps_before": 6, "steps_after": 4, "last_run": "2026-02-27T00:00:00+00:00"}}),
    )


def test_brief_sections_and_sources():
    b = full().brief()
    assert b.what_changed and b.risks and b.decisions_needed and b.next_30_days
    for section in (b.what_changed, b.risks, b.decisions_needed, b.next_30_days):
        assert all(line.source for line in section)
    assert any("Dubai ranks 2nd" in l.text for l in b.what_changed)
    assert any("40 days" in l.text and "demo date" in l.text for l in b.risks)
    assert b.ai_generated is False and b.status == "draft"


def test_decisions_are_at_most_three_and_match_the_saved_data():
    b = full().brief()
    assert 1 <= len(b.decisions_needed) <= 3
    assert any("Drone dispatch" in l.text and "Adopt" in l.text and "4.1" in l.text for l in b.decisions_needed)
    assert any("P-1" in l.text for l in b.decisions_needed)


def test_brief_is_capped_at_250_words():
    long = "word " * 120
    many = Saved({f"K{i}": {"rag": "Red", "rag_reason": long, "last_run": "2026-02-27T00:00:00+00:00",
                            "initiative": {"code": f"P-{i}", "title": "T"}} for i in range(5)})
    b = make(followup=many).brief()
    assert b.word_count <= 250


def test_empty_system_still_gives_a_brief():
    b = make().brief()
    assert b.what_changed[0].text == "Nothing has been run yet."
    assert any("Run the modules" in l.text for l in b.next_30_days)


def test_delayed_initiative_is_a_risk_when_nothing_is_checked():
    b = make().brief()
    assert any("P-1" in l.text for l in b.risks)


def test_api():
    c = TestClient(create_app())
    app = c.app
    app.dependency_overrides[get_executive_service] = lambda: full()
    assert c.get("/agents/executive/brief").json()["word_count"] > 0
    r = c.get("/agents/executive/data-quality").json()
    assert r["warnings"] >= 1 and len(r["modules"]) == 7


def test_a_long_benchmark_insight_is_shortened_not_other_lines_dropped():
    long = "Dubai ranks 2nd of 5 on safety. " + "More detail about the comparison. " * 40
    res = NS(last_run="2026-02-25T00:00:00+00:00", indicators=[1], insight=long, data_checks=[])
    svc = make(benchmark=NS(get=lambda: res), best_practices=bp(),
               process=Saved({"OPS-001": {"process": {"name": "Monitoring"}, "maturity": {"overall": 3.0},
                                          "steps_before": 6, "steps_after": 4, "last_run": "2026-02-27T00:00:00+00:00"}}))
    b = svc.brief()
    assert b.what_changed[0].text == "Dubai ranks 2nd of 5 on safety."
    sources = [l.source for l in b.what_changed]
    assert "Agent 3 Best Practices" in sources and "Agent 4 Process Analyst" in sources


def test_the_demo_sample_process_is_not_in_the_brief():
    svc = make(process=Saved({"SAMPLE-LEAVE": {"process": {"name": "Sample SOP"}, "maturity": {"overall": 2.0},
                                               "steps_before": 8, "steps_after": 6, "last_run": "2026-02-27T00:00:00+00:00"}}))
    assert not any("Sample SOP" in l.text for l in svc.brief().what_changed)
