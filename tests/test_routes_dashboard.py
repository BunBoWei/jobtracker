def test_dashboard_empty(client):
    r = client.get("/dashboard")
    assert r.status_code == 200 and "too early" in r.text and "0 of 8" in r.text


def test_dashboard_numbers_and_rule_message(client, store, seed):
    (store.data_dir).mkdir(parents=True)
    (store.data_dir / "settings.json").write_text('{"weekly_target": 4, "positive_rule": {"per": 3, "min_positive": 1}}')
    for i in range(3):
        seed(company=f"Co{i}", status="Applied", date_applied="2026-10-13", cv_variant="Java backend", source="Seek")
    seed(company="Win", status="Interview", date_applied="2026-10-12", cv_variant="Platform", source="LinkedIn")
    html = client.get("/dashboard").text
    assert "4 of 4" in html          # this week applied vs target
    assert "25%" in html             # 1 positive of 4 submitted
    assert "Java backend" in html and "LinkedIn" in html and "backend" in html
    assert "new Chart" in html


def test_dashboard_cv_problem_message(client, store, seed):
    (store.data_dir).mkdir(parents=True)
    (store.data_dir / "settings.json").write_text('{"positive_rule": {"per": 3, "min_positive": 1}}')
    for i in range(3):
        seed(company=f"Co{i}", status="Rejected", date_applied="2026-10-01")
    assert "CV problem, not the market" in client.get("/dashboard").text
