"""Type of work (full-time, part-time, ...) and region filters."""
import pytest
from fastapi.testclient import TestClient

from job_tracker import stats
from job_tracker.models import APP_COLUMNS, job_type_of, region_of
from job_tracker.server import create_app
from job_tracker.storage import write_csv


@pytest.fixture
def client(store):
    return TestClient(create_app(store.data_dir, today=store.today))


@pytest.mark.parametrize("location, region", [
    ("Wollongong, NSW", "Wollongong/Illawarra"),
    ("Shellharbour NSW", "Wollongong/Illawarra"),
    ("Sydney NSW", "Sydney"),
    ("Macquarie Park, Sydney", "Sydney"),
    ("Sydney, NSW (remote)", "Remote"),
    ("Remote (AU), Melbourne", "Remote"),
    ("Newcastle, NSW", "Other NSW"),
    ("Parkville, Melbourne VIC", "Interstate"),
    ("Perth, WA", "Interstate"),
    ("Brisbane, QLD", "Interstate"),
    ("Auckland", "Other"),
    ("", "Unknown"),
])
def test_region_of(location, region):
    assert region_of(location) == region


def test_job_type_defaults_to_full_time():
    assert job_type_of({}) == "Full-time"
    assert job_type_of({"job_type": ""}) == "Full-time"
    assert job_type_of({"job_type": "nonsense"}) == "Full-time"
    assert job_type_of({"job_type": "Casual"}) == "Casual"


def test_filter_rows_by_region_and_job_type():
    rows = [
        {"id": "a1", "company": "A", "role": "x", "notes": "", "location": "Wollongong NSW", "job_type": "Part-time", "last_update": "2026-10-01"},
        {"id": "a2", "company": "B", "role": "x", "notes": "", "location": "Sydney", "job_type": "", "last_update": "2026-10-02"},
        {"id": "a3", "company": "C", "role": "x", "notes": "", "location": "Dapto", "job_type": "Casual", "last_update": "2026-10-03"},
    ]
    ids = lambda **kw: sorted(r["id"] for r in stats.filter_rows(rows, **kw))
    assert ids(region="Wollongong/Illawarra") == ["a1", "a3"]
    assert ids(job_types=["Full-time"]) == ["a2"]
    assert ids(job_types=["Part-time", "Casual"]) == ["a1", "a3"]
    assert ids(region="Wollongong/Illawarra", job_types=["Casual"]) == ["a3"]


def test_dashboard_defaults_to_full_time_and_checkboxes_widen_it(client, seed):
    seed(company="Dev", status="Interview", date_applied="2026-10-13")                      # blank type = full-time
    seed(company="Cafe", status="Applied", date_applied="2026-10-13", job_type="Part-time")
    html = client.get("/dashboard").text
    assert "1 of 8" in html and "100%" in html          # only the full-time job counts
    assert 'value="Full-time" checked' in html and 'value="Part-time" checked' not in html
    both = client.get("/dashboard?job_type=Full-time&job_type=Part-time").text
    assert "2 of 8" in both and "50%" in both
    part = client.get("/dashboard?job_type=Part-time").text
    assert "1 of 8" in part and "0%" in part


def test_all_page_filters_by_region_and_type(client, seed):
    seed(company="LocalCafe", location="Wollongong NSW", job_type="Casual")
    seed(company="CityBank", location="Sydney NSW")
    html = client.get("/all?region=Wollongong/Illawarra").text
    assert "LocalCafe" in html and "CityBank" not in html
    html = client.get("/all?job_type=Full-time").text
    assert "CityBank" in html and "LocalCafe" not in html


def test_queue_filters_by_region(client, seed):
    seed(company="LocalCafe", location="Wollongong NSW", job_type="Part-time")
    seed(company="CityBank", location="Sydney NSW")
    html = client.get("/?region=Wollongong/Illawarra").text
    assert "LocalCafe" in html and "CityBank" not in html


def test_add_form_saves_job_type(client, store):
    r = client.post("/add", data={"company": "Pub", "role": "Bar staff", "job_type": "Casual"}, follow_redirects=False)
    assert r.status_code == 303
    _, rows = store.read_apps()
    assert rows[-1]["job_type"] == "Casual"


def test_old_csv_without_job_type_column_still_works(client, store):
    store.data_dir.mkdir(parents=True)
    old_header = [c for c in APP_COLUMNS if c != "job_type"]
    write_csv(store.data_dir / "applications.csv", old_header,
              [{"id": "a0001", "status": "Applied", "company": "OldCo", "role": "Dev", "date_applied": "2026-10-13"}])
    header, rows = store.read_apps()
    assert "job_type" in header and job_type_of(rows[0]) == "Full-time"
    assert "1 of 8" in client.get("/dashboard").text


def test_detail_save_does_not_rewrite_unchanged_job_type(client, store, seed):
    app_id = seed(company="Dev")
    client.post(f"/a/{app_id}/details", data={"notes": "hi", "orig_notes": "", "job_type": "Full-time", "orig_job_type": "Full-time"})
    assert store.get_app(app_id)["job_type"] == ""          # untouched: still counts as full-time
    client.post(f"/a/{app_id}/details", data={"job_type": "Contract", "orig_job_type": "Full-time"})
    assert store.get_app(app_id)["job_type"] == "Contract"
