from datetime import timedelta

from conftest import TODAY


def test_detail_shows_job_and_timeline_newest_first(client, store, seed):
    app_id = seed(company="Acme", role="Dev")
    client.post(f"/a/{app_id}/status", data={"status": "Interview", "note": "phone screen"})
    client.post(f"/a/{app_id}/status", data={"status": "Final", "note": "onsite"})
    html = client.get(f"/a/{app_id}").text
    assert "Acme" in html and html.index("onsite") < html.index("phone screen")
    events = store.read_events()[1]
    assert [e["to_status"] for e in events if e["application_id"] == app_id] == ["Ready", "Interview", "Final"]  # never deleted


def test_status_button_updates_status_and_rejects_non_stage(client, store, seed):
    app_id = seed(status="Applied")
    r = client.post(f"/a/{app_id}/status", data={"status": "Offer"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/a/{app_id}"
    assert store.get_app(app_id)["status"] == "Offer"
    assert client.post(f"/a/{app_id}/status", data={"status": "Ready"}).status_code == 400


def test_ad_text_saved_and_shown_with_tailor_prefix(client, store, seed):
    app_id = seed(company="Acme", role="Dev")
    client.post(f"/a/{app_id}/ad", data={"ad_text": "We need <b>Java</b>"})
    assert store.read_ad(app_id) == "We need <b>Java</b>"
    html = client.get(f"/a/{app_id}").text
    assert "We need &lt;b&gt;Java&lt;/b&gt;" in html
    assert f"Tailor my CV for Acme - Dev (tracker id {app_id}). Job ad:" in html


def test_details_edit_next_action(client, store, seed):
    app_id = seed()
    client.post(f"/a/{app_id}/details", data={"next_action": "Email recruiter", "next_action_date": "2026-10-20"})
    row = store.get_app(app_id)
    assert (row["next_action"], row["next_action_date"]) == ("Email recruiter", "2026-10-20")
    assert client.post(f"/a/{app_id}/details", data={"next_action_date": "tomorrow"}).status_code == 400


def test_duplicate_banner(client, seed):
    old = seed(company="Acme Pty Ltd", role="Backend Engineer", status="Applied",
               date_applied=(TODAY - timedelta(days=20)).isoformat())
    new = seed(company="ACME Limited", role="Backend Engineer (Java)")
    html = client.get(f"/a/{new}").text
    assert "Possible duplicate" in html and f'href="/a/{old}"' in html
    assert "Possible duplicate" not in client.get(f"/a/{old}").text or f'href="/a/{new}"' in client.get(f"/a/{old}").text


def test_no_duplicate_banner_for_old_or_different_jobs(client, seed):
    seed(company="Acme", role="Backend Engineer", status="Applied", date_applied=(TODAY - timedelta(days=90)).isoformat())
    seed(company="Acme", role="Marketing Manager")
    new = seed(company="Acme", role="Backend Engineer")
    assert "Possible duplicate" not in client.get(f"/a/{new}").text


def test_detail_unknown_id_is_404_and_hostile_link_not_rendered(client, seed):
    assert client.get("/a/a9999").status_code == 404
    app_id = seed(job_link="javascript:alert(1)", notes="<img src=x onerror=alert(1)>")
    html = client.get(f"/a/{app_id}").text
    assert 'href="javascript:' not in html and "<img src=x" not in html
