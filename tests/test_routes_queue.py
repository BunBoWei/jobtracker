import json

from conftest import HX
from job_tracker import routes


def test_health(client):
    assert client.get("/health").json() == {"app": "job-tracker", "ok": True}


def test_first_run_pages_render_empty(client, store):
    assert not store.data_dir.exists()
    r = client.get("/")
    assert r.status_code == 200 and "No jobs waiting" in r.text


def test_queue_lists_only_ready_grouped_by_priority(client, seed):
    seed(company="LowCo", priority="Low")
    seed(company="HighCo", priority="High")
    seed(company="NoPri")
    seed(company="DoneCo", status="Applied")
    html = client.get("/").text
    assert "DoneCo" not in html
    assert html.index("HighCo") < html.index("LowCo") < html.index("NoPri")


def test_queue_escapes_html_and_hides_unsafe_links(client, seed):
    seed(company="<script>alert(1)</script>", job_link="javascript:alert(1)", notes="x")
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "javascript:alert" not in html
    seed(company="Safe", job_link="https://example.com/job/1")
    assert 'href="https://example.com/job/1"' in client.get("/").text


def test_apply_via_htmx_returns_empty_fragment_and_saves_ad(client, store, seed):
    app_id = seed()
    r = client.post(f"/a/{app_id}/apply", data={"ad_text": "the ad"}, headers=HX)
    assert r.status_code == 200 and r.text == ""
    row = store.get_app(app_id)
    assert row["status"] == "Applied" and row["date_applied"] == "2026-10-14"
    assert store.read_ad(app_id) == "the ad"


def test_apply_without_htmx_redirects(client, store, seed):
    app_id = seed()
    r = client.post(f"/a/{app_id}/apply", data={}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_skip_with_fixed_reason(client, store, seed):
    app_id = seed()
    assert client.post(f"/a/{app_id}/skip", data={"reason": ".NET/C# required"}, headers=HX).status_code == 200
    row = store.get_app(app_id)
    assert (row["status"], row["skip_reason"]) == ("Skipped", ".NET/C# required")


def test_skip_other_needs_text(client, store, seed):
    app_id = seed()
    assert client.post(f"/a/{app_id}/skip", data={"reason": "other (type it)", "other_text": " "}, headers=HX).status_code == 400
    assert store.get_app(app_id)["status"] == "Ready"
    client.post(f"/a/{app_id}/skip", data={"reason": "other (type it)", "other_text": "pays too little"}, headers=HX)
    assert store.get_app(app_id)["skip_reason"] == "pays too little"


def test_skip_rejects_made_up_reason_and_unknown_id(client, seed):
    app_id = seed()
    assert client.post(f"/a/{app_id}/skip", data={"reason": "because"}).status_code == 400
    assert client.post("/a/a9999/skip", data={"reason": "too senior"}).status_code == 404


def test_open_cv(client, store, seed, monkeypatch):
    opened = []
    monkeypatch.setattr(routes, "open_file", opened.append)
    cv = store.data_dir.parent / "Tailored CVs" / "cv.pdf"
    cv.parent.mkdir(parents=True)
    cv.write_bytes(b"%PDF")
    ok = seed(cv_file="Tailored CVs/cv.pdf")
    assert client.post(f"/a/{ok}/open-cv").status_code == 200
    assert opened == [cv.resolve()]

    missing = seed(cv_file="Tailored CVs/nope.pdf")
    r = client.post(f"/a/{missing}/open-cv")
    assert r.status_code == 404 and "not found" in r.text
    assert client.post(f"/a/{seed()}/open-cv").status_code == 404           # no cv_file set
    escape = seed(cv_file="../../etc/hosts")
    assert client.post(f"/a/{escape}/open-cv").status_code == 400            # outside the Employment folder
    assert len(opened) == 1


def test_skip_panel_and_export(client, store, seed):
    for reason in ("too senior", "too senior", "security clearance"):
        client.post(f"/a/{seed()}/skip", data={"reason": reason}, headers=HX)
    html = client.get("/").text
    assert "too senior" in html and "security clearance" in html
    r = client.post("/skip-reasons/export", headers=HX)
    assert r.status_code == 200 and "skip_reasons.json" in r.text
    data = json.loads((store.data_dir / "skip_reasons.json").read_text(encoding="utf-8"))
    assert data["reasons"][0]["reason"] == "too senior" and data["reasons"][0]["count"] == 2


def test_corrupt_csv_shows_error_page_and_file_unchanged(client, store):
    store.data_dir.mkdir(parents=True)
    store.apps_path.write_text('id,status\na0001,"broken\n', encoding="utf-8")
    before = store.apps_path.read_bytes()
    for method, url in [("get", "/"), ("post", "/a/a0001/apply")]:
        r = getattr(client, method)(url)
        assert r.status_code == 500 and "applications.csv" in r.text and "not changed" in r.text
    assert store.apps_path.read_bytes() == before


def test_unprioritised_group_heading_reads_naturally(client, seed):
    seed(company="NoPri")
    html = client.get("/").text
    assert "<h2>Unprioritised</h2>" in html and "Unprioritised priority" not in html
