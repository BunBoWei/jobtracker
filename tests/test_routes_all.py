def test_all_lists_everything_with_count_and_unknown_status_row(client, seed):
    seed(company="One")
    seed(company="Two", status="Applied")
    html = client.get("/all").text
    assert "2 results" in html and "One" in html and "Two" in html


def test_all_and_add_render_on_first_run(client, store):
    assert not store.data_dir.exists()
    for path in ("/all", "/add", "/followups"):  # /dashboard is covered in Task 9
        assert client.get(path).status_code == 200, path
    assert "0 results" in client.get("/all").text


def test_all_filters_and_sort(client, seed):
    seed(company="Alpha", status="Applied", source="Seek", cv_variant="Platform", date_applied="2026-10-01", notes="remote friendly")
    seed(company="Beta", status="Rejected", source="LinkedIn", cv_variant="Go", date_applied="2026-09-01")
    html = client.get("/all", params={"q": "remote"}).text
    assert "Alpha" in html and "Beta" not in html and "1 result" in html
    assert "Beta" not in client.get("/all", params={"status": "Applied"}).text
    assert "Alpha" not in client.get("/all", params={"source": "LinkedIn"}).text
    assert "Alpha" not in client.get("/all", params={"cv_variant": "Go"}).text
    assert "Beta" not in client.get("/all", params={"date_from": "2026-09-15"}).text
    asc = client.get("/all", params={"sort": "company", "dir": "asc"}).text
    assert asc.index("Alpha") < asc.index("Beta")
    desc = client.get("/all", params={"sort": "company", "dir": "desc"}).text
    assert desc.index("Beta") < desc.index("Alpha")
    assert "0 results" in client.get("/all", params={"q": "zzz"}).text


def test_all_survives_bad_params_and_odd_rows(client, store, seed):
    with store.transaction() as tx:  # a scan-written row: the service layer would (rightly) refuse this status
        tx.add_app({"company": "Odd", "role": "R", "status": "Pending", "date_applied": "unknown"})
    r = client.get("/all", params={"sort": "evil", "dir": "sideways", "date_from": "garbage"})
    assert r.status_code == 200 and "Odd" in r.text


def test_add_form_creates_application(client, store):
    assert client.get("/add").status_code == 200
    r = client.post("/add", data={"company": "NewCo", "role": "Engineer", "status": "Applied", "source": "Company site",
                                  "job_link": "https://example.com"}, follow_redirects=False)
    assert r.status_code == 303
    app_id = r.headers["location"].rsplit("/", 1)[1]
    row = store.get_app(app_id)
    assert (row["company"], row["status"], row["date_applied"]) == ("NewCo", "Applied", "2026-10-14")


def test_add_duplicate_shows_banner_but_is_not_blocked(client, seed):
    seed(company="Acme", role="Backend Engineer", status="Applied", date_applied="2026-10-01")
    r = client.post("/add", data={"company": "Acme Pty Ltd", "role": "Backend Engineer", "status": "Ready"}, follow_redirects=True)
    assert r.status_code == 200 and "Possible duplicate" in r.text


def test_add_requires_company_and_role(client):
    assert client.post("/add", data={"company": "", "role": "x", "status": "Ready"}).status_code == 400
