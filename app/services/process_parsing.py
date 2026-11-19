"""Checks on what the model read from an SOP. The maturity average and the step counts are computed here."""

from __future__ import annotations

import re

from app.schemas.process import ISSUE_TYPES
from app.services.agent_common import quote_in, whole_between
from app.services.horizon_parsing import load_json_object
from app.services.trends_parsing import shorten

NOT_STATED = {"", "null", "none", "n/a", "not stated", "unknown", "-"}


def _text_or_none(value, limit=120) -> str | None:
    text = shorten(value, limit)
    return None if text.lower() in NOT_STATED else text


def parse_analysis(text: str | None, sop_text: str, rubric: list[dict]) -> dict | None:
    """The checked analysis, or None if the answer was not readable."""
    data = load_json_object(text)
    if not data or not isinstance(data.get("steps"), list):
        return None

    # steps: sequential ids so that everything else can refer to them safely
    steps, id_map = [], {}
    for i, raw in enumerate(data["steps"], start=1):
        if not isinstance(raw, dict):
            continue
        action = shorten(raw.get("action"), 250)
        if not action:
            continue
        new_id = f"S{len(steps) + 1}"
        id_map[str(raw.get("id") or f"S{i}").strip().upper()] = new_id
        steps.append(
            {
                "id": new_id,
                "action": action,
                "owner": _text_or_none(raw.get("owner")),
                "system": _text_or_none(raw.get("system")),
                "est_time": _text_or_none(raw.get("est_time"), 60),
            }
        )

    def known(ids) -> list[str]:
        out = []
        for item in ids or []:
            mapped = id_map.get(str(item).strip().upper())
            if mapped and mapped not in out:
                out.append(mapped)
        return out

    issues = []
    for raw in data.get("issues") or []:
        if not isinstance(raw, dict):
            continue
        description = shorten(raw.get("description"), 300)
        step_ids = known(raw.get("step_ids"))
        evidence = shorten(raw.get("evidence"), 250) or None
        verified = bool(evidence) and quote_in(sop_text, evidence)
        if not description or not (step_ids or verified):
            continue  # an issue about nothing we can point to
        kind = str(raw.get("type") or "").strip().lower()
        issues.append(
            {
                "type": kind if kind in ISSUE_TYPES else "other",
                "description": description,
                "step_ids": step_ids,
                "evidence": evidence,
                "evidence_verified": verified,
            }
        )

    # maturity: one score per rubric dimension. A score without a real quote counts as level 1.
    by_id = {d["id"]: d for d in rubric}
    scored: dict[str, dict] = {}
    for raw in data.get("maturity") or []:
        if not isinstance(raw, dict):
            continue
        dim = str(raw.get("dimension") or "").strip().lower()
        level = whole_between(raw.get("level"), 1, 5)
        if dim not in by_id or dim in scored or level is None:
            continue
        evidence = shorten(raw.get("evidence"), 250) or None
        verified = bool(evidence) and quote_in(sop_text, evidence)
        reason = shorten(raw.get("reason"), 300)
        if not verified and level > 1:
            reason = f"Lowered to 1: no line of the SOP supports a higher score. (The AI had said {level}.) {reason}".strip()
            level = 1
        scored[dim] = {
            "id": dim,
            "name": by_id[dim]["name"],
            "level": level,
            "reason": reason or "No reason given.",
            "evidence": evidence if verified else None,
            "evidence_verified": verified,
        }
    dimensions = []
    for d in rubric:
        dimensions.append(
            scored.get(d["id"])
            or {
                "id": d["id"], "name": d["name"], "level": 1, "reason": "Not assessed: no evidence found in the SOP.",
                "evidence": None, "evidence_verified": False,
            }
        )
    overall = round(sum(d["level"] for d in dimensions) / len(dimensions), 1) if dimensions else None

    flow = []
    for raw in data.get("proposed_flow") or []:
        if isinstance(raw, dict) and shorten(raw.get("action"), 250):
            flow.append(
                {"id": f"N{len(flow) + 1}", "action": shorten(raw.get("action"), 250), "replaces": known(raw.get("replaces"))}
            )

    clarifications = [c for c in (shorten(x, 250) for x in data.get("needs_clarification") or []) if c]
    if steps and all(s["owner"] is None for s in steps):
        clarifications.append("No step names an owner.")
    if steps and all(s["est_time"] is None for s in steps):
        clarifications.append("No step states a time, so no time saving can be calculated.")

    saving = shorten(data.get("expected_time_saving"), 200)
    if not any(s["est_time"] for s in steps):
        saving = "Cannot be estimated: the SOP states no times."
    return {
        "steps": steps,
        "issues": issues,
        "maturity": {"overall": overall, "dimensions": dimensions},
        "steps_before": len(steps),
        "steps_after": len(flow),
        "proposed_flow": flow,
        "needs_clarification": list(dict.fromkeys(clarifications)),
        "expected_time_saving": saving or "Not stated.",
    }
