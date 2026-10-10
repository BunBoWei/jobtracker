from datetime import date, timedelta

import pytest

from job_tracker import service, stats
from conftest import TODAY

SETTINGS = {"weekly_target": 8, "auto_reject_days": 30, "positive_rule": {"per": 5, "min_positive": 1}}


def r(id, status, date_applied="", cv="", source="", role="Backend Engineer", nad="", **extra):
    base = {c: "" for c in ["id", "status", "date_applied", "cv_variant", "source", "role", "company",
                            "next_action_date", "created_at", "last_update", "notes", "skip_reason", "priority"]}
    base.update(id=id, status=status, date_applied=date_applied, cv_variant=cv, source=source, role=role,
                company=f"Co {id}", next_action_date=nad)
    base.update(extra)
    return base


FIXTURE = [
    r("a1", "Applied", "2026-10-12", "Java backend", "Seek", "Backend Engineer"),
    r("a2", "Applied", "2026-10-13", "Java backend", "LinkedIn", "Platform Engineer", nad="2026-10-10"),
    r("a3", "Interview", "2026-10-05", "Platform", "Seek", "DevOps Engineer"),
    r("a4", "Rejected", "2026-10-06", "Java backend", "Seek", "Backend Developer", nad="2026-10-01"),
    r("a5", "Offer", "2026-09-29", "Platform", "LinkedIn", "Full Stack Developer"),
    r("a6", "Ready", cv="Original", source="Seek", created_at="2026-10-10"),
    r("a7", "Skipped", role="Data Analyst"),
    r("a8", "Assessment", "2026-10-12", "Grad", "Seek", "Graduate Engineer"),
]


def by_key(rows):
    return {x["key"]: (x["submitted"], x["positive"]) for x in rows}


def test_dashboard_numbers():
    d = stats.dashboard(FIXTURE, SETTINGS, TODAY)
    assert (d["submitted"], d["positive"], d["rate"]) == (6, 3, pytest.approx(0.5))
    assert (d["week_applied"], d["week_target"]) == (3, 8)
    assert d["rule"] == "ok"
    assert d["funnel"] == {"submitted": 6, "positive": 3, "interview": 2, "offer": 1}
    assert (d["ready"], d["overdue"]) == (1, 1)
    assert len(d["weekly"]) == 12 and d["weekly"][-1] == {"week": "2026-10-12", "count": 3}
    counts = {w["week"]: w["count"] for w in d["weekly"]}
    assert counts["2026-10-05"] == 2 and counts["2026-09-28"] == 1
    assert sum(counts.values()) == 6
    assert by_key(d["by_cv"]) == {"Java backend": (3, 0), "Platform": (2, 2), "Grad": (1, 1)}
    assert by_key(d["by_source"]) == {"Seek": (4, 2), "LinkedIn": (2, 1)}
    assert by_key(d["by_role"]) == {"backend": (2, 0), "platform/devops/cloud": (2, 1), "full-stack": (1, 1), "graduate": (1, 1)}
    assert d["by_cv"][0]["key"] == "Java backend"  # sorted by submitted desc
    assert {x["key"]: x["rate"] for x in d["by_cv"]}["Platform"] == pytest.approx(1.0)


def test_rule_check_states():
    few = [r(f"a{i}", "Applied", "2026-10-01") for i in range(4)]
    assert stats.dashboard(few, SETTINGS, TODAY)["rule"] == "too_early"  # 4 < per=5
    bad = [r(f"a{i}", "Applied", "2026-10-01") for i in range(10)]  # 0 positive in 10: 0*5 < 1*10
    assert stats.dashboard(bad, SETTINGS, TODAY)["rule"] == "cv_problem"
    edge = bad[:-2] + [r("p1", "Interview", "2026-10-01"), r("p2", "Interview", "2026-10-01")]
    assert stats.dashboard(edge, SETTINGS, TODAY)["rule"] == "ok"  # 2*5=10 >= 1*10


def test_dashboard_empty_and_hostile_rows():
    d = stats.dashboard([], SETTINGS, TODAY)
    assert d["submitted"] == 0 and d["rate"] is None and d["rule"] == "too_early"
    assert d["by_cv"] == [] and len(d["weekly"]) == 12
    weird = [r("a1", "Pending", "2026-10-12"), r("a2", "Applied", "unknown"), r("a3", "Applied", "")]
    d = stats.dashboard(weird, SETTINGS, TODAY)
    assert d["submitted"] == 2 and d["week_applied"] == 0 and sum(w["count"] for w in d["weekly"]) == 0
    assert by_key(d["by_cv"]) == {"(none)": (2, 0)}


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Graduate Software Engineer", "graduate"),
        ("Senior Backend Engineer (Java)", "backend"),
        ("DevOps Engineer", "platform/devops/cloud"),
        ("Cloud Platform Engineer", "platform/devops/cloud"),
        ("Full Stack Developer", "full-stack"),
        ("Full-Stack AI Developer", "AI/ML"),
        ("ML Engineer", "AI/ML"),
        ("Product Manager", "other"),
        ("", "other"),
    ],
)
def test_role_type(title, expected):
    assert stats.role_type(title) == expected


def test_followups_lists():
    rows = [
        r("o1", "Applied", "2026-10-01", nad="2026-10-10"),       # overdue
        r("o2", "Interview", "2026-10-01", nad="2026-10-13"),     # overdue
        r("t0", "Applied", "2026-10-01", nad="2026-10-14"),       # due today
        r("t7", "Applied", "2026-10-01", nad="2026-10-21"),       # due in 7 days (inclusive)
        r("t8", "Applied", "2026-10-01", nad="2026-10-22"),       # beyond the week
        r("x1", "Rejected", "2026-10-01", nad="2026-10-01"),      # terminal: ignored
        r("x2", "Applied", "2026-10-01", nad="soon"),             # bad date: ignored
        r("g25", "Applied", "2026-09-19"),                         # 25 days: about to reject
        r("g29", "Applied", "2026-09-15"),                         # 29 days
        r("g30", "Applied", "2026-09-14"),                         # 30 days: rule candidate, not "about to"
        r("g24", "Applied", "2026-09-20"),                         # 24 days: too early
    ]
    f = stats.followups(rows, [], SETTINGS, TODAY)
    assert [x["id"] for x in f["overdue"]] == ["o1", "o2"]
    assert [x["id"] for x in f["due_week"]] == ["t0", "t7"]
    assert [x["id"] for x in f["about_to_reject"]] == ["g29", "g25"]  # oldest first
    assert [x["id"] for x in f["rule_candidates"]] == ["g30"]


def test_skip_tally_window_and_examples():
    rows = [
        r("s1", "Skipped", skip_reason="too senior", last_update="2026-10-10", company="A", role="X"),
        r("s2", "Skipped", skip_reason="too senior", last_update="2026-09-14", company="B", role="Y"),  # 30 days: in
        r("s3", "Skipped", skip_reason="too senior", last_update="2026-09-13", company="C", role="Z"),  # 31 days: out
        r("s4", "Skipped", skip_reason="Needs Go", last_update="2026-10-01", company="D", role="W"),
        r("s5", "Skipped", skip_reason="", last_update="2026-10-01"),
        r("s6", "Applied", skip_reason="stale", last_update="2026-10-01"),
    ]
    tally = stats.skip_tally(rows, TODAY)
    assert [(t["reason"], t["count"]) for t in tally] == [("too senior", 2), ("Needs Go", 1)]
    assert tally[0]["examples"] == [{"company": "A", "role": "X"}, {"company": "B", "role": "Y"}]


def test_export_skip_reasons_writes_json(store, seed):
    import json

    app_id = seed(company="Acme", role="Dev")
    service.skip(store, app_id, "too junior")
    path = service.export_skip_reasons(store)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "skip_reasons.json" and data["window_days"] == 30
    assert data["reasons"] == [{"reason": "too junior", "count": 1, "examples": [{"company": "Acme", "role": "Dev"}]}]


def test_filter_rows():
    rows = [
        r("a1", "Applied", "2026-10-01", "Java backend", "Seek", "Backend Engineer", company="Acme", notes="great team"),
        r("a2", "Rejected", "2026-09-01", "Platform", "LinkedIn", "DevOps", company="Beta"),
        r("a3", "Ready", cv="Platform", source="Seek", role="SRE", company="Gamma", created_at="2026-10-10"),
    ]
    ids = lambda **kw: [x["id"] for x in stats.filter_rows(rows, **kw)]
    assert sorted(ids()) == ["a1", "a2", "a3"]
    assert ids(q="GREAT") == ["a1"]                       # searches notes, case-insensitive
    assert ids(q="gamma") == ["a3"]
    assert sorted(ids(status="Applied")) == ["a1"]
    assert sorted(ids(source="Seek")) == ["a1", "a3"]
    assert sorted(ids(cv_variant="Platform")) == ["a2", "a3"]
    assert ids(date_from="2026-09-15", date_to="2026-10-05") == ["a1"]
    assert ids(date_from="2026-10-10") == ["a3"]           # falls back to created_at
    assert ids(sort="company", direction="asc") == ["a1", "a2", "a3"]
    assert ids(sort="company", direction="desc") == ["a3", "a2", "a1"]
    assert ids(sort="nonsense") is not None                # unknown sort column falls back, never crashes
