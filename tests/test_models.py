from datetime import date, timedelta

import pytest

from job_tracker.models import (
    find_duplicates,
    normalise_company,
    parse_date,
    rule_eligible,
    stage_changed_ids,
    tailor_prefix,
)

TODAY = date(2026, 10, 14)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Atlassian Pty. Ltd.", "atlassian"),
        ("ANZ Limited", "anz"),
        ("  Canva   PTY LTD ", "canva"),
        ("Smith & Co Pty Ltd", "smith co"),
        ("Pty Ltd", ""),
    ],
)
def test_normalise_company(raw, expected):
    assert normalise_company(raw) == expected


def test_parse_date_tolerates_garbage():
    assert parse_date("2026-10-14") == date(2026, 10, 14)
    assert parse_date("") is None
    assert parse_date("unknown") is None
    assert parse_date(None) is None


def _row(id, company, role, days_ago, field="date_applied"):
    return {"id": id, "company": company, "role": role, field: (TODAY - timedelta(days=days_ago)).isoformat()}


def test_duplicate_matches_normalised_company_and_similar_role():
    rows = [_row("a1", "Acme Pty Ltd", "Backend Engineer", 10)]
    hits = find_duplicates(rows, "ACME Limited", "Backend Engineer (Java)", TODAY)
    assert [r["id"] for r in hits] == ["a1"]


def test_duplicate_role_threshold_is_inclusive():
    from difflib import SequenceMatcher

    rows = [_row("a1", "Acme", "Backend Engineer", 1)]
    role = "Backend Developer"
    ratio = SequenceMatcher(None, role.lower(), "backend engineer").ratio()
    assert find_duplicates(rows, "Acme", role, TODAY, threshold=ratio)
    assert not find_duplicates(rows, "Acme", role, TODAY, threshold=ratio + 0.001)


def test_duplicate_different_role_not_flagged():
    rows = [_row("a1", "Acme", "Marketing Manager", 1)]
    assert find_duplicates(rows, "Acme", "Backend Engineer", TODAY) == []


def test_duplicate_window_is_60_days_inclusive():
    rows = [_row("in", "Acme", "Backend Engineer", 60), _row("out", "Acme", "Backend Engineer", 61)]
    assert [r["id"] for r in find_duplicates(rows, "Acme", "Backend Engineer", TODAY)] == ["in"]


def test_duplicate_falls_back_to_created_at_and_skips_undated_and_self():
    rows = [
        _row("c1", "Acme", "Backend Engineer", 5, field="created_at"),
        {"id": "u1", "company": "Acme", "role": "Backend Engineer"},
        _row("me", "Acme", "Backend Engineer", 1),
    ]
    hits = find_duplicates(rows, "Acme", "Backend Engineer", TODAY, exclude_id="me")
    assert [r["id"] for r in hits] == ["c1"]


def test_rule_eligible_edges():
    def row(age):
        return {"id": "a1", "status": "Applied", "date_applied": (TODAY - timedelta(days=age)).isoformat()}

    assert not rule_eligible(row(29), set(), TODAY, 30)
    assert rule_eligible(row(30), set(), TODAY, 30)
    assert rule_eligible(row(31), set(), TODAY, 30)
    assert not rule_eligible(row(31), {"a1"}, TODAY, 30)  # already moved through a stage
    assert not rule_eligible({"id": "a1", "status": "Applied", "date_applied": ""}, set(), TODAY, 30)
    assert not rule_eligible({**row(40), "status": "Interview"}, set(), TODAY, 30)


def test_stage_changed_ignores_applied_and_ready_events():
    events = [
        {"application_id": "a1", "to_status": "Ready"},
        {"application_id": "a1", "to_status": "Applied"},
        {"application_id": "a2", "to_status": "Interview"},
    ]
    assert stage_changed_ids(events) == {"a2"}


def test_tailor_prefix():
    row = {"id": "a0007", "company": "Acme", "role": "Backend Engineer"}
    assert tailor_prefix(row) == "Tailor my CV for Acme - Backend Engineer (tracker id a0007). Job ad:"
