import csv
from datetime import date

import pytest

from job_tracker.storage import AppNotFound, CorruptCsvError, Store, read_csv


def _add(store, **fields):
    """Add one row via a raw transaction (service.py does not exist until Task 3)."""
    with store.transaction() as tx:
        return tx.add_app({"company": "Acme", "role": "Dev", **fields})["id"]


def test_first_run_reads_empty_without_creating_files(store):
    header, rows = store.read_apps()
    assert rows == [] and "status" in header
    assert not store.apps_path.exists()
    assert store.settings()["weekly_target"] == 8


def test_round_trip_and_unknown_columns_preserved(store):
    store.data_dir.mkdir(parents=True)
    store.apps_path.write_text(
        "id,status,company,role,scan_score\na0001,Ready,Acme,Dev,0.93\n", encoding="utf-8"
    )
    with store.transaction() as tx:
        tx.find("a0001")["status"] = "Applied"
    header, rows = store.read_apps()
    assert "scan_score" in header
    assert rows[0]["scan_score"] == "0.93" and rows[0]["status"] == "Applied"
    # known columns missing from the old file were appended, not lost
    assert "cv_variant" in header and rows[0]["cv_variant"] == ""


def test_new_rows_get_every_column_and_sequential_ids(store):
    with store.transaction() as tx:
        a = tx.add_app({"company": "A", "role": "R"})
        b = tx.add_app({"company": "B", "role": "R"})
    assert (a["id"], b["id"]) == ("a0001", "a0002")
    with store.transaction() as tx:
        assert tx.add_app({"company": "C", "role": "R"})["id"] == "a0003"


def test_csv_quirks_round_trip(store):
    store.data_dir.mkdir(parents=True)
    raw = 'id,status,company,role,notes\r\na0001,Ready,"Acme, Inc",Dev,"line1\r\nline2 ""quoted"""\r\n'
    store.apps_path.write_bytes(b"\xef\xbb\xbf" + raw.encode("utf-8"))  # BOM + CRLF
    with store.transaction() as tx:
        tx.find("a0001")["status"] = "Applied"
    _, rows = store.read_apps()
    assert rows[0]["company"] == "Acme, Inc"
    assert rows[0]["notes"] == 'line1\r\nline2 "quoted"'


def test_unknown_id_raises(store):
    with pytest.raises(AppNotFound):
        with store.transaction() as tx:
            tx.find("a9999")


def test_exception_inside_transaction_writes_nothing(store):
    _add(store)
    before = store.apps_path.read_bytes()
    with pytest.raises(RuntimeError):
        with store.transaction() as tx:
            tx.find("a0001")["status"] = "Applied"
            raise RuntimeError("boom")
    assert store.apps_path.read_bytes() == before


@pytest.mark.parametrize(
    "content",
    [
        'id,status\na0001,"unterminated\n',
        "id,status\na0001,Ready,EXTRA\n",
        "id,status,company\na0001,Ready\n",
        "status,company\nReady,Acme\n",  # no id column
        "id,id\na1,a2\n",
    ],
)
def test_corrupt_csv_raises_and_is_never_overwritten(store, content):
    store.data_dir.mkdir(parents=True)
    store.apps_path.write_text(content, encoding="utf-8")
    before = store.apps_path.read_bytes()
    with pytest.raises(CorruptCsvError) as err:
        with store.transaction():
            pass
    assert "applications.csv" in str(err.value)
    assert store.apps_path.read_bytes() == before


def test_invalid_utf8_is_corrupt(store):
    store.data_dir.mkdir(parents=True)
    store.apps_path.write_bytes(b"id,status\na1,\xff\xfe\n")
    with pytest.raises(CorruptCsvError):
        read_csv(store.apps_path, ["id"])


def test_no_temp_files_left_behind(store):
    _add(store)
    assert [p.name for p in store.data_dir.iterdir() if p.name.endswith(".tmp")] == []


def test_daily_backup_once_per_day_and_prunes_to_14(tmp_path):
    current = [date(2026, 9, 1)]
    s = Store(tmp_path / "d", today=lambda: current[0])
    with s.transaction() as tx:  # day 1, no files yet
        tx.add_app({"company": "A", "role": "R"})
    assert (s.data_dir / "backups" / "2026-09-01").is_dir()
    assert not (s.data_dir / "backups" / "2026-09-01" / "applications.csv").exists()
    state_after_first = s.apps_path.read_text(encoding="utf-8")

    with s.transaction() as tx:  # same day again: no new backup contents
        tx.add_app({"company": "B", "role": "R"})
    assert not (s.data_dir / "backups" / "2026-09-01" / "applications.csv").exists()

    current[0] = date(2026, 9, 2)
    with s.transaction() as tx:
        tx.add_app({"company": "C", "role": "R"})
    backed_up = (s.data_dir / "backups" / "2026-09-02" / "applications.csv").read_text(encoding="utf-8")
    assert backed_up != state_after_first and "B" in backed_up and "C" not in backed_up

    for day in range(3, 21):  # 18 more days
        current[0] = date(2026, 9, day)
        with s.transaction() as tx:
            tx.add_app({"company": f"D{day}", "role": "R"})
    kept = sorted(p.name for p in (s.data_dir / "backups").iterdir())
    assert len(kept) == 14 and kept[-1] == "2026-09-20" and kept[0] == "2026-09-07"


def test_ad_files(store):
    assert store.read_ad("a0001") == ""
    store.write_ad("a0001", "Hello\r\nWorld é")
    assert store.read_ad("a0001") == "Hello\r\nWorld é"
    assert (store.data_dir / "job_ads" / "a0001.md").exists()
    with pytest.raises(ValueError):
        store.write_ad("../evil", "x")


def test_settings_merge_defaults(store):
    store.data_dir.mkdir(parents=True)
    (store.data_dir / "settings.json").write_text('{"weekly_target": 5, "positive_rule": {"per": 10}}')
    s = store.settings()
    assert s["weekly_target"] == 5 and s["auto_reject_days"] == 30
    assert s["positive_rule"] == {"per": 10, "min_positive": 4}


def test_corrupt_settings_raises(store):
    store.data_dir.mkdir(parents=True)
    (store.data_dir / "settings.json").write_text("{nope")
    with pytest.raises(CorruptCsvError):
        store.settings()
