"""Pure computations over rows. No file access."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .models import (POSITIVE, SUBMITTED, TERMINAL, job_type_of, parse_date, region_of, row_date,
                     rule_eligible, stage_changed_ids)

SORTABLE = ("company", "role", "location", "job_type", "status", "source", "cv_variant", "date_applied", "last_update")

_ROLE_RULES = [
    ("graduate", r"\bgraduate\b|\bgrad\b|\bentry[- ]level\b"),
    ("AI/ML", r"\bai\b|\bml\b|machine learning|\bllm\b|data scien"),
    ("platform/devops/cloud", r"devops|platform|cloud|\bsre\b|site reliability|infrastructure"),
    ("full-stack", r"full[- ]?stack"),
    ("backend", r"back[- ]?end|\bjava\b|\bpython\b|\bgolang\b|\bapi\b|\bserver"),
]


def role_type(title: str) -> str:
    t = (title or "").lower()
    for name, pattern in _ROLE_RULES:
        if re.search(pattern, t):
            return name
    return "other"


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _breakdown(rows: list[dict], key_of) -> list[dict]:
    groups: dict[str, list[int]] = {}
    for r in rows:
        if r.get("status") in SUBMITTED:
            g = groups.setdefault(key_of(r) or "(none)", [0, 0])
            g[0] += 1
            g[1] += r["status"] in POSITIVE
    out = [{"key": k, "submitted": s, "positive": p, "rate": p / s} for k, (s, p) in groups.items()]
    return sorted(out, key=lambda x: (-x["submitted"], x["key"]))


def _active_with_date(rows):
    for r in rows:
        d = parse_date(r.get("next_action_date"))
        if d is not None and r.get("status") not in TERMINAL:
            yield d, r


def dashboard(rows: list[dict], settings: dict, today: date, job_types=None) -> dict:
    """job_types: only count rows of these types (None = every row)."""
    if job_types:
        rows = [r for r in rows if job_type_of(r) in job_types]
    submitted = [r for r in rows if r.get("status") in SUBMITTED]
    positive = [r for r in submitted if r["status"] in POSITIVE]
    monday = _monday(today)
    applied_dates = [d for d in (parse_date(r.get("date_applied")) for r in submitted) if d]

    starts = [monday - timedelta(weeks=i) for i in range(11, -1, -1)]
    weekly = [{"week": s.isoformat(), "count": sum(s <= d < s + timedelta(days=7) for d in applied_dates)} for s in starts]

    per, minimum = settings["positive_rule"]["per"], settings["positive_rule"]["min_positive"]
    if len(submitted) < per:
        rule = "too_early"
    elif len(positive) * per < minimum * len(submitted):
        rule = "cv_problem"
    else:
        rule = "ok"

    return {
        "week_applied": sum(monday <= d <= today for d in applied_dates),
        "week_target": settings["weekly_target"],
        "submitted": len(submitted),
        "positive": len(positive),
        "rate": len(positive) / len(submitted) if submitted else None,
        "rule": rule, "rule_per": per, "rule_min": minimum,
        "weekly": weekly,
        "funnel": {
            "submitted": len(submitted), "positive": len(positive),
            "interview": sum(r["status"] in ("Interview", "Final", "Offer") for r in submitted),
            "offer": sum(r["status"] == "Offer" for r in submitted),
        },
        "by_cv": _breakdown(rows, lambda r: r.get("cv_variant", "").strip()),
        "by_source": _breakdown(rows, lambda r: r.get("source", "").strip()),
        "by_role": _breakdown(rows, lambda r: role_type(r.get("role", ""))),
        "by_region": _breakdown(rows, lambda r: region_of(r.get("location", ""))),
        "ready": sum(r.get("status") == "Ready" for r in rows),
        "overdue": sum(d < today for d, _ in _active_with_date(rows)),
    }


def followups(rows: list[dict], events: list[dict], settings: dict, today: date) -> dict:
    days = settings["auto_reject_days"]
    dated = sorted(_active_with_date(rows), key=lambda t: (t[0], t[1]["id"]))
    changed = stage_changed_ids(events)
    applied = [(r, (today - parse_date(r["date_applied"])).days) for r in rows
               if r.get("status") == "Applied" and parse_date(r.get("date_applied"))]
    return {
        "overdue": [r for d, r in dated if d < today],
        "due_week": [r for d, r in dated if today <= d <= today + timedelta(days=7)],
        "about_to_reject": [r for r, age in sorted(applied, key=lambda t: -t[1]) if days - 5 <= age < days],
        "rule_candidates": [r for r in rows if rule_eligible(r, changed, today, days)],
    }


def skip_tally(rows: list[dict], today: date, days: int = 30, max_examples: int = 3) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        reason = (r.get("skip_reason") or "").strip()
        d = parse_date(r.get("last_update"))
        if r.get("status") == "Skipped" and reason and d and (today - d).days <= days:
            groups.setdefault(reason, []).append(r)
    out = [
        {"reason": k, "count": len(v), "examples": [{"company": x["company"], "role": x["role"]} for x in v[:max_examples]]}
        for k, v in groups.items()
    ]
    return sorted(out, key=lambda g: (-g["count"], g["reason"]))


def filter_rows(rows, q="", status="", source="", cv_variant="", date_from="", date_to="",
                sort="last_update", direction="desc", region="", job_types=None) -> list[dict]:
    needle = q.strip().lower()
    start, end = parse_date(date_from), parse_date(date_to)
    out = []
    for r in rows:
        if needle and needle not in " ".join(r.get(k, "") for k in ("company", "role", "notes")).lower():
            continue
        if status and r.get("status") != status:
            continue
        if source and r.get("source") != source:
            continue
        if cv_variant and r.get("cv_variant") != cv_variant:
            continue
        if region and region_of(r.get("location", "")) != region:
            continue
        if job_types and job_type_of(r) not in job_types:
            continue
        if start or end:
            d = row_date(r)
            if d is None or (start and d < start) or (end and d > end):
                continue
        out.append(r)
    column = sort if sort in SORTABLE else "last_update"
    # blanks always sort last regardless of direction
    filled = sorted((r for r in out if r.get(column)), key=lambda r: r[column].lower(), reverse=direction == "desc")
    return filled + [r for r in out if not r.get(column)]
