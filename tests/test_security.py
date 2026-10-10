from job_tracker import routes
from job_tracker.storage import DataDirMissing, Store


def test_cross_site_post_is_rejected_and_changes_nothing(client, store, seed):
    app_id = seed()
    r = client.post(f"/a/{app_id}/skip", data={"reason": "too senior"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403 and store.get_app(app_id)["status"] == "Ready"
    assert client.post(f"/a/{app_id}/skip", data={"reason": "too senior"}, headers={"Origin": "null"}).status_code == 403


def test_same_origin_post_is_allowed(client, store, seed):
    app_id = seed()
    for origin in ("http://localhost:8765", "http://127.0.0.1:8765"):
        other = seed()
        r = client.post(f"/a/{other}/skip", data={"reason": "too senior"}, headers={"Origin": origin}, follow_redirects=False)
        assert r.status_code == 303, origin


def test_foreign_host_header_is_rejected_even_for_get(client):
    assert client.get("/", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:8765"}).status_code == 200


def test_open_cv_only_opens_document_types(client, store, seed, monkeypatch):
    opened = []
    monkeypatch.setattr(routes, "open_file", opened.append)
    folder = store.data_dir.parent / "Tailored CVs"
    folder.mkdir(parents=True)
    (folder / "run.command").write_text("#!/bin/sh\necho hi\n")
    (folder / "ok.docx").write_bytes(b"x")
    bad = seed(cv_file="Tailored CVs/run.command")
    good = seed(cv_file="Tailored CVs/ok.docx")
    assert client.post(f"/a/{bad}/open-cv").status_code == 400 and opened == []
    assert client.post(f"/a/{good}/open-cv").status_code == 200 and len(opened) == 1


def test_missing_drive_folder_is_an_error_not_a_silent_local_copy(tmp_path, monkeypatch):
    store = Store(tmp_path / "GoogleDrive-not-mounted" / "My Drive" / "Job Tracker")
    for call in (store.read_apps, store.read_events, store.settings):
        try:
            call()
            raise AssertionError("expected DataDirMissing")
        except DataDirMissing as e:
            assert "Google Drive" in str(e)
    try:
        with store.transaction():
            pass
        raise AssertionError("expected DataDirMissing")
    except DataDirMissing:
        pass
    assert not (tmp_path / "GoogleDrive-not-mounted").exists()


def test_missing_drive_folder_shows_error_page(tmp_path):
    from fastapi.testclient import TestClient

    from job_tracker.server import create_app

    c = TestClient(create_app(tmp_path / "nope" / "Job Tracker"))
    r = c.get("/")
    assert r.status_code == 500 and "Google Drive" in r.text and not (tmp_path / "nope").exists()
