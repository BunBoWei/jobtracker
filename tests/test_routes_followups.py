from datetime import timedelta

from conftest import HX, TODAY


def days_ago(n):
    return (TODAY - timedelta(days=n)).isoformat()


def test_followups_page_sections(client, seed):
    seed(company="OverdueCo", status="Applied", date_applied=days_ago(5), next_action="Call", next_action_date=days_ago(2))
    seed(company="SoonCo", status="Applied", date_applied=days_ago(5), next_action="Email", next_action_date=(TODAY + timedelta(days=3)).isoformat())
    seed(company="AgingCo", status="Applied", date_applied=days_ago(27))
    seed(company="FreshCo", status="Applied", date_applied=days_ago(3))
    html = client.get("/followups").text
    assert "OverdueCo" in html and 'class="overdue"' in html
    assert "SoonCo" in html and "AgingCo" in html and "FreshCo" not in html
    assert html.index("Overdue") < html.index("SoonCo")


def test_mark_rejected_button_removes_row(client, store, seed):
    app_id = seed(status="Applied", date_applied=days_ago(27))
    r = client.post(f"/a/{app_id}/reject", headers=HX)
    assert r.status_code == 200 and r.text == ""
    assert store.get_app(app_id)["status"] == "Rejected"
    last = [e for e in store.read_events()[1] if e["application_id"] == app_id][-1]
    assert last["note"] == "no reply"


def test_apply_rule_button(client, store, seed):
    old = seed(status="Applied", date_applied=days_ago(31))
    young = seed(status="Applied", date_applied=days_ago(29))
    r = client.post("/followups/apply-rule", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/followups?rejected=1"
    assert store.get_app(old)["status"] == "Rejected" and store.get_app(young)["status"] == "Applied"
    assert "1 job" in client.get("/followups?rejected=1").text


def test_followups_empty(client):
    assert client.get("/followups").status_code == 200
