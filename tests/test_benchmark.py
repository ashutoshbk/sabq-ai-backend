"""Benchmarking Agent: what it keeps, what it drops, what it ranks, what it explains."""

import asyncio
import functools
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_benchmark_service
from app.core.jobs import refresh_feed_then_agent
from app.core.store import JsonFileStore
from app.main import create_app
from app.schemas.benchmark import BenchmarkIndicator
from app.services.benchmark_parsing import (
    _number,
    build_group,
    check_baseline,
    normalize,
    parse_findings,
)
from app.services.benchmark_service import BenchmarkService, load_entities, load_indicators
from tests.conftest import FakeClient, FakeResponse, make_settings

ENTITIES, VALID_TYPES = load_entities()
INDICATORS = load_indicators()
IND = {i.id: i for i in INDICATORS}
START = datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc)

EGDI, IMD, GALLUP, OXFORD, TRUST = (
    "un-egdi", "imd-wcr", "gallup-law-order", "oxford-ai-readiness", "police-public-trust",
)


def sync(fn):
    """The project has no async test plugin, so run an async test body ourselves."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- the fake model -------------------------------------------------------

def hit(subject, url, value=None, rank=None, rank_of=None, year=2024, smin=0, smax=1, **extra):
    r = {
        "subject": subject, "found": True, "metric": "Index value", "value": value,
        "scale_min": smin, "scale_max": smax, "higher_is_better": True, "rank": rank,
        "rank_of": rank_of, "year": year, "source_url": url, "evidence": "Table 3",
        "confidence": "high",
    }
    r.update(extra)
    return r


def miss(subject):
    return {"subject": subject, "found": False}


def answer(*results, cited=None):
    cited = [r["source_url"] for r in results if r.get("found") and r.get("source_url")] if cited is None else cited
    return FakeResponse(json.dumps({"results": list(results)}), cited)


class Brain:
    """Answers the search calls. The agent makes no other model call."""

    def __init__(self, fake):
        self.queues: dict[str, list] = {}
        self.fail: set[str] = set()
        self.searches: list[dict] = []
        self.other_calls = 0
        fake.responses.handler = self

    def add(self, indicator_id, *responses):
        self.queues.setdefault(IND[indicator_id].name, []).extend(responses)

    def __call__(self, kwargs):
        if not kwargs.get("tools"):
            self.other_calls += 1
            return FakeResponse("{}")
        self.searches.append(kwargs)
        name = next(n for n in (i.name for i in INDICATORS) if f"Indicator: {n}" in kwargs["input"])
        if name in self.fail:
            raise RuntimeError("search down")
        queue = self.queues.get(name, [])
        return queue.pop(0) if queue else answer()


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


def make(store=None, **settings):
    fake = FakeClient()
    brain = Brain(fake)
    clock = Clock()
    service = BenchmarkService(
        fake, make_settings(**settings), ENTITIES, VALID_TYPES, INDICATORS, store=store, now_fn=clock
    )
    return service, brain, clock


def client_for(service):
    app = create_app()
    app.dependency_overrides[get_benchmark_service] = lambda: service
    return TestClient(app)


UN = "https://publicadministration.un.org/egovkb/en-us/Data-Center"
IMDURL = "https://www.imd.org/centers/wcc/world-competitiveness-center/rankings/world-competitiveness-ranking/"


def three_countries(brain, year=2024):
    brain.add(EGDI, answer(
        hit("Singapore", UN + "?c=sg", value=0.9, year=year),
        hit("South Korea", UN + "?c=kr", value=0.95, year=year),
        hit("United Arab Emirates", UN + "?c=ae", value=0.85, year=year),
    ))


def entity(response, entity_id):
    return next(e for e in response.entities if e.id == entity_id)


def finding(response, entity_id, indicator_id):
    return next(f for f in entity(response, entity_id).findings if f.indicator_id == indicator_id)


# ---- what is searched -----------------------------------------------------

@sync
async def test_one_search_per_indicator_on_its_own_trusted_domains():
    service, brain, _ = make()

    await service.run(force=True)

    assert len(brain.searches) == len(INDICATORS) == 5
    by_name = {i.name: i for i in INDICATORS}
    for call in brain.searches:
        name = next(n for n in by_name if f"Indicator: {n}" in call["input"])
        assert call["tools"][0]["filters"]["allowed_domains"] == by_name[name].domains
        assert call["tool_choice"] == "required"
    egdi = next(c for c in brain.searches if "Indicator: UN E-Government" in c["input"])["input"]
    assert "- Singapore" in egdi and "- South Korea" in egdi and "- United Arab Emirates" in egdi
    trust = next(c for c in brain.searches if "public trust or safety survey" in c["input"])["input"]
    assert "- Dubai Police" in trust and "- Abu Dhabi Police" in trust
    assert "Singapore" not in trust.split("Subjects")[1]  # surveys apply to police and ministry only


def test_the_config_holds_no_links():
    text = (json.dumps([i.model_dump() for i in INDICATORS]))
    assert "http" not in text


# ---- keeping real figures, dropping the rest ---------------------------------

@sync
async def test_a_found_figure_is_kept_with_its_source_and_config_name():
    service, brain, _ = make()
    three_countries(brain)

    r = await service.run(force=True)

    f = finding(r, "singapore-planning", EGDI)
    assert f.status == "found" and f.value == 0.9 and f.year == 2024
    assert f.source_url.startswith(UN) and f.source_name == "United Nations (UN DESA)"
    assert f.describes == "Singapore (the country as a whole)"
    assert "not this organization" in f.note


@sync
async def test_figures_without_a_cited_page_or_on_the_wrong_domain_are_dropped():
    service, brain, _ = make()
    brain.add(EGDI, answer(
        hit("Singapore", UN, value=0.9),                      # cited and on a trusted domain
        hit("South Korea", "https://not-cited.un.org/x", value=0.95),  # real domain, never cited
        hit("United Arab Emirates", "https://evil.example/un", value=0.85),  # cited but wrong domain
        cited=[UN, "https://evil.example/un"],
    ))

    r = await service.run(force=True)

    assert finding(r, "singapore-planning", EGDI).status == "found"
    assert finding(r, "south-korea-planning", EGDI).status == "not_found"
    assert finding(r, "uae-moi", EGDI).status == "not_found"


@sync
async def test_look_alike_domains_are_rejected():
    service, brain, _ = make()
    fake_url = "https://un.org.evil.example/egdi"
    brain.add(EGDI, answer(hit("Singapore", fake_url, value=0.9), cited=[fake_url]))

    r = await service.run(force=True)

    assert finding(r, "singapore-planning", EGDI).status == "not_found"


@sync
async def test_a_value_outside_its_own_scale_is_dropped_but_a_rank_can_stand():
    service, brain, _ = make()
    brain.add(EGDI, answer(
        hit("Singapore", UN, value=5.0, rank=3, rank_of=193),   # 5 on a 0-1 scale
        hit("South Korea", UN + "?kr", value=7.0),                 # nothing usable left
    ))

    r = await service.run(force=True)

    sg = finding(r, "singapore-planning", EGDI)
    assert sg.status == "found" and sg.value is None and sg.rank == 3
    assert finding(r, "south-korea-planning", EGDI).status == "not_found"


@sync
async def test_unreadable_figures_and_unknown_subjects_are_ignored():
    service, brain, _ = make()
    brain.add(EGDI, answer(
        hit("Singapore", UN, value="n/a"),                         # not a number
        hit("Atlantis", UN + "?x", value=0.5),                      # not a subject we asked about
        hit("south korea", UN + "?kr", value="0.95", year=2099),   # name match ignores case; bad year dropped
    ))

    r = await service.run(force=True)

    assert finding(r, "singapore-planning", EGDI).status == "not_found"
    kr = finding(r, "south-korea-planning", EGDI)
    assert kr.value == 0.95 and kr.year is None


# ---- comparing -------------------------------------------------------------

@sync
async def test_same_indicator_and_year_are_ranked_together_by_computed_scores():
    service, brain, _ = make()
    three_countries(brain)

    r = await service.run(force=True)

    ind = next(i for i in r.indicators if i.id == EGDI)
    assert ind.compared and ind.year == 2024
    assert [(x.subject, x.group_rank) for x in ind.ranking] == [
        ("South Korea", 1), ("Singapore", 2), ("United Arab Emirates", 3),
    ]
    f = finding(r, "singapore-planning", EGDI)
    assert f.comparable and f.group_rank == 2 and f.normalized == 90.0
    assert "Ranked with South Korea, Singapore, United Arab Emirates" in f.note


@sync
async def test_every_uae_entity_shares_the_country_figure():
    service, brain, _ = make()
    three_countries(brain)

    r = await service.run(force=True)

    for entity_id in ("dubai-police", "abu-dhabi-police", "uae-moi", "uae-federal-planning"):
        f = finding(r, entity_id, EGDI)
        assert f.value == 0.85 and f.describes.startswith("United Arab Emirates")


@sync
async def test_different_years_are_not_ranked_together():
    service, brain, _ = make()
    brain.add(EGDI, answer(
        hit("Singapore", UN, value=0.9, year=2024),
        hit("South Korea", UN + "?kr", value=0.95, year=2024),
        hit("United Arab Emirates", UN + "?ae", value=0.99, year=2022),
    ))

    r = await service.run(force=True)

    ae = finding(r, "uae-moi", EGDI)
    assert not ae.comparable and ae.group_rank is None and "2022 edition" in ae.note
    assert finding(r, "singapore-planning", EGDI).comparable


@sync
async def test_one_lonely_figure_has_nothing_to_be_ranked_against():
    service, brain, _ = make()
    brain.add(IMD, answer(hit("Singapore", IMDURL, value=100, smax=100, rank=1, rank_of=69)))

    r = await service.run(force=True)

    f = finding(r, "singapore-planning", IMD)
    assert not f.comparable and "nothing to rank it against" in f.note
    assert next(i for i in r.indicators if i.id == IMD).compared is False


@sync
async def test_rank_basis_and_score_basis_are_not_mixed():
    service, brain, _ = make()
    brain.add(IMD, answer(
        hit("Singapore", IMDURL, value=100, smax=100, year=2026),
        hit("South Korea", IMDURL + "?kr", rank=27, rank_of=69, smin=None, smax=None, year=2026),
        hit("United Arab Emirates", IMDURL + "?ae", value=95, smax=100, year=2026),
    ))

    r = await service.run(force=True)

    kr = finding(r, "south-korea-planning", IMD)
    assert not kr.comparable and "different basis" in kr.note
    assert finding(r, "singapore-planning", IMD).comparable


@sync
async def test_surveys_are_shown_but_never_ranked():
    service, brain, _ = make()
    base = "https://www.dubaipolice.gov.ae/survey"
    brain.add(TRUST, answer(
        hit("Dubai Police", base, value=99.9, smax=100, year=2024),
        hit("Abu Dhabi Police", "https://www.adpolice.gov.ae/s", value=97, smax=100, year=2024),
    ))

    r = await service.run(force=True)

    f = finding(r, "dubai-police", TRUST)
    assert f.status == "found" and f.value == 99.9 and not f.comparable
    assert "not ranked against other organizations" in f.note
    assert [e for e in r.entities if e.id == "singapore-planning"][0].findings == [] or all(
        x.indicator_id != TRUST for x in entity(r, "singapore-planning").findings
    )
    ind = next(i for i in r.indicators if i.id == TRUST)
    assert not ind.compared and "not ranked against each other" in ind.note


# ---- not found, keeping, replacing -----------------------------------------

@sync
async def test_not_found_is_an_honest_answer():
    service, brain, _ = make()
    brain.add(GALLUP, answer(miss("Singapore")))

    r = await service.run(force=True)

    f = finding(r, "singapore-planning", GALLUP)
    assert f.status == "not_found" and "No published figure" in f.note and f.value is None


@sync
async def test_a_later_not_found_never_wipes_a_figure_we_have():
    service, brain, _ = make()
    three_countries(brain)
    await service.run(force=True)
    brain.add(EGDI, answer(miss("Singapore"), miss("South Korea"), miss("United Arab Emirates")))

    r = await service.run(force=True)

    assert finding(r, "singapore-planning", EGDI).value == 0.9


@sync
async def test_a_newer_edition_replaces_an_older_one_but_not_the_reverse():
    service, brain, _ = make()
    brain.add(EGDI, answer(hit("Singapore", UN, value=0.9, year=2024)))
    await service.run(force=True)

    brain.add(EGDI, answer(hit("Singapore", UN + "?new", value=0.92, year=2026)))
    newer = await service.run(force=True)
    assert finding(newer, "singapore-planning", EGDI).year == 2026

    brain.add(EGDI, answer(hit("Singapore", UN + "?old", value=0.5, year=2020)))
    older = await service.run(force=True)
    assert finding(older, "singapore-planning", EGDI).year == 2026


# ---- failures ----------------------------------------------------------------

@sync
async def test_one_failing_search_keeps_the_others_and_says_so():
    service, brain, _ = make()
    three_countries(brain)
    brain.fail = {IND[IMD].name}

    r = await service.run(force=True)

    assert finding(r, "singapore-planning", EGDI).status == "found"
    assert r.warning and "IMD World Competitiveness Ranking" in r.warning
    assert r.last_run is not None


@sync
async def test_an_unreadable_answer_is_a_failure_not_a_not_found():
    service, brain, _ = make()
    brain.add(EGDI, FakeResponse("sorry, I cannot do that"))

    r = await service.run(force=True)

    assert "Government Development" in r.warning or "UN E-Government" in r.warning
    assert all(f.indicator_id != EGDI for e in r.entities for f in e.findings)


@sync
async def test_everything_failing_raises():
    service, brain, _ = make()
    brain.fail = {i.name for i in INDICATORS}

    with pytest.raises(RuntimeError):
        await service.run(force=True)


# ---- when it runs --------------------------------------------------------------

@sync
async def test_never_run_means_due_and_fresh_results_are_not_searched_again():
    service, brain, clock = make()
    three_countries(brain)

    await service.run()  # first time: due
    calls = len(brain.searches)
    await service.run()  # fresh: skipped
    assert len(brain.searches) == calls

    clock.now = START + timedelta(days=89)
    await service.run()
    assert len(brain.searches) == calls

    clock.now = START + timedelta(days=91)
    await service.run()
    assert len(brain.searches) == calls * 2


@sync
async def test_an_indicator_that_failed_before_is_due_again():
    service, brain, _ = make()
    brain.fail = {IND[IMD].name}
    await service.run(force=True)
    calls = len(brain.searches)

    brain.fail = set()
    await service.run()  # not older than 90 days, but IMD has nothing saved

    assert len(brain.searches) > calls


def test_reading_never_searches_and_says_when_nothing_is_collected():
    service, brain, _ = make()

    r = service.get()

    assert brain.searches == []
    assert r.last_run is None and "Nothing has been collected yet" in r.warning
    assert all(e.findings == [] for e in r.entities)
    assert len(r.entities) == 6 and r.entities[0].baseline.score == 88


# ---- saving ------------------------------------------------------------------

@sync
async def test_results_survive_a_restart(tmp_path):
    store = JsonFileStore(tmp_path / "benchmark.json")
    service, brain, _ = make(store=store)
    three_countries(brain)
    await service.run(force=True)

    again, brain2, _ = make(store=JsonFileStore(tmp_path / "benchmark.json"))

    r = again.get()
    assert finding(r, "singapore-planning", EGDI).value == 0.9
    assert r.insight and "South Korea" in r.insight
    assert brain2.searches == []


# ---- the insight ---------------------------------------------------------------

@sync
async def test_insight_is_built_in_code_and_matches_the_ranking():
    service, brain, _ = make()
    three_countries(brain)

    r = await service.run(force=True)

    # South Korea has the highest EGDI in this data, so the insight must say so, and never Singapore.
    assert "South Korea in UN E-Government Development Index (EGDI) (2024)" in r.insight
    assert "Singapore in UN" not in r.insight
    assert "#3 of 3" in r.insight  # the UAE
    assert "demo data" in r.insight
    assert brain.other_calls == 0  # no model writes it


@sync
async def test_no_figures_found_means_no_insight():
    service, brain, _ = make()

    r = await service.run(force=True)

    assert r.insight is None


def test_insight_names_the_shared_leader_when_one_subject_leads_everything():
    from app.schemas.benchmark import IndicatorResult, RankedSubject
    from app.services.benchmark_parsing import build_insight

    def ind(i):
        return IndicatorResult(
            id=f"i{i}", name=f"Index {i}", publisher="P", level="country", year=2024, compared=True, note="n",
            ranking=[RankedSubject(subject="Aland", group_rank=1), RankedSubject(subject="Bland", group_rank=2)],
        )

    class F:
        status = "found"
        level = "country"

    class E:
        is_self = False
        findings = [F()]

    text = build_insight([ind(1), ind(2)], [E()], [])
    assert "Aland ranks first in all 2 indicators" in text


@sync
async def test_an_old_figure_gets_an_out_of_date_note():
    service, brain, _ = make()
    three_countries(brain, year=2019)

    r = await service.run(force=True)

    assert "may be out of date" in finding(r, "singapore-planning", EGDI).note


# ---- checks on the platform table -------------------------------------------------

def test_the_demo_table_checks():
    checks = check_baseline(ENTITIES, VALID_TYPES)
    by_code = {}
    for c in checks:
        by_code.setdefault(c.code, []).append(c)

    inconsistent = by_code["rank_inconsistent"]
    assert len(inconsistent) == 1
    assert {"south-korea-planning", "uae-federal-planning"} == set(inconsistent[0].entities)
    assert "higher score (93)" in inconsistent[0].message and inconsistent[0].severity == "warning"

    assert {c.entities[0] for c in by_code["invalid_type"]} == {
        "singapore-planning", "uae-federal-planning", "south-korea-planning",
    }
    assert by_code["rank_scope_unclear"][0].severity == "info"
    assert len(by_code["baseline_unsourced"][0].entities) == 6


def test_a_clean_table_raises_no_rank_or_type_warnings():
    rows = [e.model_copy(update={"type": "Reference"}) for e in ENTITIES
            if e.id in ("dubai-police", "abu-dhabi-police")]
    codes = {c.code for c in check_baseline(rows, VALID_TYPES)}
    assert codes == {"baseline_unsourced"}


# ---- the API ---------------------------------------------------------------------

def test_api_read_refresh_and_indicators():
    service, brain, _ = make()
    three_countries(brain)
    tc = client_for(service)

    first = tc.get("/agents/benchmark").json()
    assert first["warning"] and brain.searches == []

    done = tc.get("/agents/benchmark?refresh=true")
    body = done.json()
    assert done.status_code == 200 and body["status"] == "draft" and body["ai_generated"] is True
    sg = next(e for e in body["entities"] if e["id"] == "singapore-planning")
    assert any(f["indicator_id"] == EGDI and f["value"] == 0.9 for f in sg["findings"])
    assert body["insight"] and body["disclaimer"]

    listing = tc.get("/agents/benchmark/indicators").json()
    assert [i["id"] for i in listing] == [EGDI, IMD, GALLUP, OXFORD, TRUST]
    assert "http" not in json.dumps(listing)


# ---- pure helpers ---------------------------------------------------------------

def test_number_reading():
    assert _number("93.4%") == 93.4 and _number("1,234") == 1234 and _number(0.5) == 0.5
    for bad in (None, True, "n/a", float("nan"), float("inf"), [1]):
        assert _number(bad) is None


def test_normalize():
    assert normalize(0.9, 0, 1, True, None, None) == (90.0, "score")
    assert normalize(30, 0, 100, False, None, None) == (70.0, "score")  # lower is better
    assert normalize(None, None, None, True, 1, 69) == (100.0, "rank")
    assert normalize(None, None, None, True, 69, 69) == (0.0, "rank")
    assert normalize(None, None, None, True, 3, None) == (None, None)
    assert normalize(5, 1, 1, True, None, None) == (None, None)  # empty scale


def test_ties_share_a_rank():
    service, _, _ = make()
    from app.schemas.benchmark import StoredFinding

    def f(subject, normalized):
        return StoredFinding(indicator_id=EGDI, subject=subject, status="found", year=2024,
                             normalized=normalized, basis="score", checked_at="x")

    group = build_group([f("A", 90), f("B", 90), f("C", 80)], IND[EGDI])
    assert group.ranks == {"A": 1, "B": 1, "C": 3}


def test_parse_findings_returns_none_for_unreadable_text():
    assert parse_findings("nope", {}, ["Singapore"], IND[EGDI], date(2026, 11, 17)) is None
    assert parse_findings(json.dumps({"results": []}), {}, ["Singapore"], IND[EGDI], date(2026, 11, 17)) == {}


def test_indicator_domain_limit_is_enforced():
    with pytest.raises(Exception):
        BenchmarkIndicator(id="x", name="x", publisher="x", level="country", hint="x",
                           domains=[f"d{i}.com" for i in range(21)])


def test_settings_defaults():
    s = make_settings()
    assert s.benchmark_refresh_days == 90 and s.benchmark_auto_run_after_refresh is True
    assert s.benchmark_model == "gpt-5.4-mini"
    with pytest.raises(Exception):
        make_settings(benchmark_refresh_days=0)


# ---- the scheduled job runs more than one agent ----------------------------------

@sync
async def test_one_failing_agent_does_not_stop_the_next():
    calls = []

    async def feed():
        calls.append("feed")

    async def horizon():
        calls.append("horizon")
        raise RuntimeError("boom")

    async def benchmark():
        calls.append("benchmark")

    await refresh_feed_then_agent(feed, horizon, benchmark)

    assert calls == ["feed", "horizon", "benchmark"]


@sync
async def test_job_skips_agents_that_are_switched_off():
    calls = []

    async def feed():
        calls.append("feed")

    async def benchmark():
        calls.append("benchmark")

    await refresh_feed_then_agent(feed, None, benchmark)

    assert calls == ["feed", "benchmark"]
