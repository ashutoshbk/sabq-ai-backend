"""Pure helpers for the Benchmarking Agent: checking what the model read, normalizing, grouping.

All numbers and rankings are computed here in plain code. The model only reads published pages
and writes a short insight from the numbers computed here.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date
from typing import Any

from app.core.resources import DomainEntry
from app.schemas.benchmark import (
    BenchmarkEntity,
    BenchmarkIndicator,
    DataCheck,
    EntityResult,
    IndicatorResult,
    StoredFinding,
)
from app.services.horizon_parsing import load_json_object
from app.services.trends_parsing import (
    clean_url,
    coerce_choice,
    collect_citation_urls,
    match_domain,
    shorten,
    url_key,
)

logger = logging.getLogger(__name__)

CONFIDENCE_LEVELS = ["high", "medium", "low"]
MIN_YEAR = 2000


def _number(value: Any) -> float | None:
    """A float from 0.95, '0.95', '93.4%' or '1,234'. Anything else (or NaN/inf) is None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.replace(",", "").replace("%", "").strip()
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _whole(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number == int(number) else None


def normalize(
    value: float | None,
    scale_min: float | None,
    scale_max: float | None,
    higher_is_better: bool,
    rank: int | None,
    rank_of: int | None,
) -> tuple[float | None, str | None]:
    """0 to 100, higher = better. Uses the score when its scale is known, else the published rank."""
    if value is not None and scale_min is not None and scale_max is not None and scale_min < scale_max:
        fraction = (value - scale_min) / (scale_max - scale_min)
        if not higher_is_better:
            fraction = 1 - fraction
        return round(fraction * 100, 1), "score"
    if rank is not None and rank_of is not None and rank_of > 1:
        return round((rank_of - rank) / (rank_of - 1) * 100, 1), "rank"
    return None, None


def parse_findings(
    text: str | None,
    dump: dict,
    subjects: list[str],
    indicator: BenchmarkIndicator,
    today: date,
) -> dict[str, dict] | None:
    """Subject -> a checked finding. Subjects the model could not support are left out.

    Returns None when the answer was not readable at all (so the caller can tell a failed
    search apart from 'nothing was found').

    A figure is kept only if: its page was cited by the search tool, the page is on one of the
    indicator's domains, and it has a number (or a rank) that fits its stated scale.
    """
    data = load_json_object(text)
    results = (data or {}).get("results")
    if not isinstance(results, list):
        return None

    cited = collect_citation_urls(dump)
    domains = [DomainEntry(domain=d, name=indicator.publisher, level=1) for d in indicator.domains]
    by_name = {s.lower(): s for s in subjects}
    found: dict[str, dict] = {}

    for raw in results:
        if not isinstance(raw, dict) or raw.get("found") is not True:
            continue
        subject = by_name.get(str(raw.get("subject") or "").strip().lower())
        if not subject or subject in found:
            continue

        url = clean_url(raw.get("source_url"))
        if not url or url_key(url) not in cited:
            logger.info("Dropped %s / %s: source was not cited by the search tool", indicator.id, subject)
            continue
        if not match_domain(url, domains):
            logger.info("Dropped %s / %s: %s is not on the indicator's domains", indicator.id, subject, url)
            continue

        value = _number(raw.get("value"))
        scale_min, scale_max = _number(raw.get("scale_min")), _number(raw.get("scale_max"))
        if scale_min is not None and scale_max is not None and scale_min < scale_max:
            if value is not None and not (scale_min <= value <= scale_max):
                logger.info("Dropped value for %s / %s: outside its own scale", indicator.id, subject)
                value = None
        else:
            scale_min = scale_max = None

        rank, rank_of = _whole(raw.get("rank")), _whole(raw.get("rank_of"))
        if rank is None or rank < 1 or (rank_of is not None and rank > rank_of):
            rank = rank_of = None
        if rank_of is not None and rank_of < 1:
            rank_of = None

        if value is None and rank is None:
            continue

        year = _whole(raw.get("year"))
        if year is not None and not (MIN_YEAR <= year <= today.year):
            year = None

        higher = raw.get("higher_is_better") is not False
        normalized, basis = normalize(value, scale_min, scale_max, higher, rank, rank_of)
        found[subject] = {
            "indicator_id": indicator.id,
            "subject": subject,
            "status": "found",
            "metric": shorten(raw.get("metric"), 120) or None,
            "value": value,
            "scale_min": scale_min,
            "scale_max": scale_max,
            "higher_is_better": higher,
            "rank": rank,
            "rank_of": rank_of,
            "year": year,
            "normalized": normalized,
            "basis": basis,
            "source_name": indicator.publisher,  # from our own config, never from the model
            "source_url": url,
            "evidence": shorten(raw.get("evidence"), 200) or None,
            "confidence": coerce_choice(raw.get("confidence"), CONFIDENCE_LEVELS, "low"),
        }
    return found


# ---- grouping: who can fairly be ranked together --------------------------

class Group:
    """Subjects of ONE indicator, with the same year and the same normalizing basis."""

    def __init__(self, year: int | None = None, basis: str | None = None):
        self.year = year
        self.basis = basis
        self.ranks: dict[str, int] = {}  # subject -> rank inside the group
        self.notes: dict[str, str] = {}  # subject -> why it was or was not ranked

    @property
    def compared(self) -> bool:
        return len(self.ranks) >= 2


def build_group(findings: list[StoredFinding], indicator: BenchmarkIndicator) -> Group:
    """Pick the largest set of findings with the same year and basis and rank them (ties share a rank)."""
    group = Group()
    found = [f for f in findings if f.status == "found"]

    if not indicator.comparable_across_entities:
        for f in found:
            group.notes[f.subject] = (
                "A published survey with its own method and wording. It is shown for information "
                "and is not ranked against other organizations."
            )
        return group

    usable = [f for f in found if f.year is not None and f.normalized is not None and f.basis]
    for f in found:
        if f not in usable:
            group.notes[f.subject] = (
                "Not ranked: the year or the scale of this figure is unclear."
            )

    buckets: dict[tuple[int, str], list[StoredFinding]] = {}
    for f in usable:
        buckets.setdefault((f.year, f.basis), []).append(f)  # type: ignore[arg-type]
    if not buckets:
        return group

    (year, basis), members = max(buckets.items(), key=lambda kv: (len(kv[1]), kv[0][0]))
    group.year, group.basis = year, basis

    for key, others in buckets.items():
        if key == (year, basis):
            continue
        for f in others:
            group.notes[f.subject] = (
                f"Not ranked with the others: it is from the {f.year} edition, "
                f"but the comparison uses {year}."
                if f.basis == basis
                else "Not ranked with the others: it is measured on a different basis (score or rank)."
            )

    if len(members) < 2:
        for f in members:
            group.notes[f.subject] = (
                "Only one subject has a comparable figure for this indicator and year, so there is nothing to rank it against."
            )
        group.year = year
        return group

    ordered = sorted(members, key=lambda f: -(f.normalized or 0))
    names = ", ".join(f.subject for f in ordered)
    for position, f in enumerate(ordered, start=1):
        # Standard competition ranking: equal scores share a rank (1, 1, 3).
        better = sum(1 for other in ordered if (other.normalized or 0) > (f.normalized or 0))
        group.ranks[f.subject] = better + 1
        group.notes[f.subject] = (
            f"Ranked with {names}: same indicator, same year ({year}), same basis."
        )
    return group


# ---- checks on the platform's own table -----------------------------------

def check_baseline(entities: list[BenchmarkEntity], valid_types: list[str]) -> list[DataCheck]:
    """Plain consistency checks on the demo table. No model involved."""
    checks: list[DataCheck] = []

    def number(rank: str | None) -> int | None:
        match = re.search(r"\d+", rank or "")
        return int(match.group()) if match else None

    by_scope: dict[str, list[BenchmarkEntity]] = {}
    for e in entities:
        by_scope.setdefault(e.scope, []).append(e)

    for scope, rows in by_scope.items():
        for a in rows:
            for b in rows:
                ra, rb = number(a.demo_rank), number(b.demo_rank)
                if None in (a.demo_score, b.demo_score, ra, rb) or a is b:
                    continue
                if a.demo_score > b.demo_score and ra > rb:  # type: ignore[operator]
                    checks.append(DataCheck(
                        code="rank_inconsistent",
                        severity="warning",
                        message=(
                            f"In the {scope} group, {a.name} has a higher score ({a.demo_score:g}) than "
                            f"{b.name} ({b.demo_score:g}) but a worse rank ({a.demo_rank} against {b.demo_rank})."
                        ),
                        entities=[a.id, b.id],
                    ))

    for e in entities:
        if e.type not in valid_types:
            checks.append(DataCheck(
                code="invalid_type",
                severity="warning",
                message=f"{e.name} has the type '{e.type}', which is not one of: {', '.join(valid_types)}.",
                entities=[e.id],
            ))

    firsts = [e for e in entities if number(e.demo_rank) == 1]
    if len({e.scope for e in firsts}) > 1 and len(firsts) > 1:
        checks.append(DataCheck(
            code="rank_scope_unclear",
            severity="info",
            message=(
                f"{len(firsts)} entities are shown as #1 ({', '.join(e.name for e in firsts)}). "
                "This is only consistent if ranks are counted inside each scope (Local, Federal, "
                "Global). Please confirm how ranks are meant to be counted."
            ),
            entities=[e.id for e in firsts],
        ))

    checks.append(DataCheck(
        code="baseline_unsourced",
        severity="info",
        message=(
            "The platform table has no source and no date for any of its scores. They are treated "
            "as demo data and are never ranked against published indicators."
        ),
        entities=[e.id for e in entities],
    ))
    return checks


# ---- the insight: built in plain code, so every word can be traced to a number ----

def build_insight(
    indicators: list[IndicatorResult],
    entities: list[EntityResult],
    checks: list[DataCheck],
) -> str | None:
    """A short summary of the computed results. No model writes it, so it cannot contradict them."""
    if not any(f.status == "found" for e in entities for f in e.findings):
        return None

    sentences: list[str] = []
    compared = [i for i in indicators if i.compared]

    if compared:
        leaders = [[r.subject for r in i.ranking if r.group_rank == 1] for i in compared]
        first = leaders[0]
        if len(compared) > 1 and all(ls == first for ls in leaders):
            sentences.append(
                f"{' and '.join(first)} ranks first in all {len(compared)} indicators that could be fairly compared."
            )
        else:
            parts = [f"{' and '.join(ls)} in {i.name} ({i.year})" for i, ls in zip(compared, leaders)]
            sentences.append("First place among the compared countries: " + "; ".join(parts) + ".")

        home = next((e for e in entities if e.is_self), None)
        if home:
            places = []
            for i in compared:
                row = next((r for r in i.ranking if r.subject == home.country), None)
                if row:
                    places.append(f"#{row.group_rank} of {len(i.ranking)} in {i.name}")
            if places:
                sentences.append(f"{home.country}, where Dubai Police operates, is " + "; ".join(places) + ".")
    else:
        sentences.append("No indicator has figures for two or more subjects with the same year and scale, so nothing is ranked.")

    surveys = [i for i in indicators if not i.ranking and i.level == "organization"]
    shown = sum(
        1 for e in entities for f in e.findings
        if f.status == "found" and f.level == "organization"
    )
    if shown:
        sentences.append(
            f"{shown} published survey figure(s) are shown for information only: surveys use different "
            "methods, so they are not ranked against each other."
        )
    del surveys

    missing: dict[str, set[str]] = {}
    for e in entities:
        for f in e.findings:
            if f.status == "not_found":
                missing.setdefault(f.indicator_name, set()).add(f.describes.replace(" (the country as a whole)", ""))
    if missing:
        sentences.append(
            "No published figure was found for: "
            + "; ".join(f"{name} ({', '.join(sorted(who))})" for name, who in missing.items())
            + "."
        )

    warnings = sum(1 for c in checks if c.severity == "warning")
    if warnings:
        sentences.append(f"The platform table itself has {warnings} data warning(s) (see data_checks).")

    sentences.append(
        "Country figures describe each country as a whole, not the organizations in the table, "
        "and the platform baseline scores are demo data."
    )
    return " ".join(sentences)
