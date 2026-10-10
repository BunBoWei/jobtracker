from datetime import timedelta

import pytest

from job_tracker import service
from job_tracker.storage import AppNotFound
from conftest import TODAY


def events_for(store, app_id):
    return [e for e in store.read_events()[1] if e["application_id"] == app_id]


def test_add_application_defaults_and_event(store):
    app_id = service.add_application(store, {"company": " Acme ", "role": "Dev"})
    row = store.get_app(app_id)
    assert row["status"] == "Ready" and row["company"] == "Acme"
    assert row["created_at"] == row["last_update"] == TODAY.isoformat()
    evs = events_for(store, app_id)
    assert [(e["from_status"], e["to_status"], e["note"]) for e in evs] == [("", "Ready", "added")]


def test_add_applied_sets_date_applied(store):
    app_id = service.add_application(store, {"company": "A", "role": "R", "status": "Applied"})
    assert store.get_app(app_id)["date_applied"] == TODAY.isoformat()


@pytest.mark.parametrize("fields", [{"company": "", "role": "R"}, {"company": "A", "role": ""}, {"company": "A", "role": "R", "status": "Nope"}])
def test_add_application_validation(store, fields):
    with pytest.raises(ValueError):
        service.add_application(store, fields)


def test_change_status_writes_exactly_one_event(store, seed):
    app_id = seed()
    before = len(store.read_events()[1])
    service.change_status(store, app_id, "Interview", "phone screen")
    evs = store.read_events()[1]
    assert len(evs) == before + 1
    last = evs[-1]
    assert (last["application_id"], last["from_status"], last["to_status"], last["note"]) == (
        app_id, "Ready", "Interview", "phone screen")
    row = store.get_app(app_id)
    assert row["status"] == "Interview" and row["last_update"] == TODAY.isoformat()


def test_change_status_rejects_unknown_status_and_unknown_id(store, seed):
    app_id = seed()
    with pytest.raises(ValueError):
        service.change_status(store, app_id, "Pending")
    with pytest.raises(AppNotFound):
        service.change_status(store, "a9999", "Applied")


def test_mark_applied_sets_dates_event_and_saves_ad(store, seed):
    app_id = seed()
    service.mark_applied(store, app_id, "The ad text")
    row = store.get_app(app_id)
    assert (row["status"], row["date_applied"], row["last_update"]) == ("Applied", TODAY.isoformat(), TODAY.isoformat())
    assert events_for(store, app_id)[-1]["to_status"] == "Applied"
    assert store.read_ad(app_id) == "The ad text"


def test_mark_applied_without_ad_writes_no_ad_file(store, seed):
    app_id = seed()
    service.mark_applied(store, app_id, "   ")
    assert not (store.data_dir / "job_ads" / f"{app_id}.md").exists()


def test_skip_writes_reason_and_event(store, seed):
    app_id = seed()
    service.skip(store, app_id, "security clearance")
    row = store.get_app(app_id)
    assert (row["status"], row["skip_reason"]) == ("Skipped", "security clearance")
    last = events_for(store, app_id)[-1]
    assert (last["from_status"], last["to_status"], last["note"]) == ("Ready", "Skipped", "security clearance")


def test_skip_requires_reason(store, seed):
    with pytest.raises(ValueError):
        service.skip(store, seed(), "  ")


def test_update_details_whitelist_and_date_validation(store, seed):
    app_id = seed()
    service.update_details(store, app_id, {"next_action": "Email", "next_action_date": "2026-10-20", "status": "Offer", "id": "zzz"})
    row = store.get_app(app_id)
    assert (row["next_action"], row["next_action_date"]) == ("Email", "2026-10-20")
    assert row["status"] == "Ready" and row["id"] == app_id  # non-editable fields ignored
    with pytest.raises(ValueError):
        service.update_details(store, app_id, {"next_action_date": "next tuesday"})
    service.update_details(store, app_id, {"next_action_date": ""})  # clearing is fine
    assert store.get_app(app_id)["next_action_date"] == ""


def test_save_ad_requires_existing_app(store, seed):
    app_id = seed()
    service.save_ad(store, app_id, "text")
    assert store.read_ad(app_id) == "text"
    with pytest.raises(AppNotFound):
        service.save_ad(store, "a9999", "x")


@pytest.mark.parametrize("age,expected", [(29, "Applied"), (30, "Rejected"), (31, "Rejected")])
def test_thirty_day_rule_edges(store, seed, age, expected):
    app_id = seed(status="Applied", date_applied=(TODAY - timedelta(days=age)).isoformat())
    rejected = service.apply_rejection_rule(store)
    assert store.get_app(app_id)["status"] == expected
    assert rejected == ([app_id] if expected == "Rejected" else [])
    if expected == "Rejected":
        last = events_for(store, app_id)[-1]
        assert (last["from_status"], last["to_status"], last["note"]) == ("Applied", "Rejected", "auto: no reply after 30 days")


def test_thirty_day_rule_skips_already_changed_and_other_statuses(store, seed):
    old = (TODAY - timedelta(days=45)).isoformat()
    moved = seed(status="Applied", date_applied=old)
    service.change_status(store, moved, "Interview")
    service.change_status(store, moved, "Applied")  # back to Applied, but it did move through a stage
    interview = seed(status="Interview", date_applied=old)
    undated = seed(status="Applied", date_applied="")
    junk = seed(status="Applied", date_applied="unknown")
    assert service.apply_rejection_rule(store) == []
    for app_id, status in [(moved, "Applied"), (interview, "Interview"), (undated, "Applied"), (junk, "Applied")]:
        assert store.get_app(app_id)["status"] == status


def test_rule_writes_one_event_each(store, seed):
    old = (TODAY - timedelta(days=40)).isoformat()
    ids = [seed(status="Applied", date_applied=old) for _ in range(3)]
    assert sorted(service.apply_rejection_rule(store)) == sorted(ids)
    for app_id in ids:
        assert [e["to_status"] for e in events_for(store, app_id)] == ["Applied", "Rejected"]


def test_mark_applied_and_skip_refuse_jobs_that_are_no_longer_ready(store, seed):
    app_id = seed(status="Applied", date_applied="2026-09-01")
    events_before = len(events_for(store, app_id))
    with pytest.raises(ValueError, match="no longer"):
        service.mark_applied(store, app_id, "ad")
    with pytest.raises(ValueError, match="no longer"):
        service.skip(store, app_id, "too senior")
    row = store.get_app(app_id)
    assert (row["status"], row["date_applied"]) == ("Applied", "2026-09-01")   # 30-day clock not restarted
    assert len(events_for(store, app_id)) == events_before
    assert store.read_ad(app_id) == ""


def test_incomplete_jobs_can_still_be_applied_or_skipped(store, seed):
    a, b = seed(status="Incomplete"), seed(status="Incomplete")
    service.mark_applied(store, a)
    service.skip(store, b, "too junior")
    assert (store.get_app(a)["status"], store.get_app(b)["status"]) == ("Applied", "Skipped")


def test_change_status_expect(store, seed):
    app_id = seed(status="Applied")
    with pytest.raises(ValueError, match="no longer"):
        service.change_status(store, app_id, "Rejected", expect={"Ready"})
    service.change_status(store, app_id, "Rejected", expect={"Applied"})
    assert store.get_app(app_id)["status"] == "Rejected"
