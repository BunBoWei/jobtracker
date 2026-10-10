import subprocess
import sys
import textwrap
import time

from job_tracker.storage import Store

SCAN_SCRIPT = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from job_tracker.storage import Store

    store = Store(Path(sys.argv[1]))
    with store.transaction() as tx:   # the scan follows the same protocol: lock, re-read, append by id
        tx.add_app({"company": "ScanCo", "role": "Scanned Role", "status": "Ready"})
    """
)


def test_external_append_between_read_and_write_survives(store):
    with store.transaction() as tx:
        row_a = tx.add_app({"company": "RowA", "role": "R"})["id"]

    scan = {}

    def start_scan_while_app_holds_lock():
        # The app has read the CSV and holds the lock; the scan starts now and must wait.
        scan["proc"] = subprocess.Popen([sys.executable, "-c", SCAN_SCRIPT, str(store.data_dir)])
        time.sleep(0.5)
        assert scan["proc"].poll() is None  # still blocked on the lock

    store.after_read = start_scan_while_app_holds_lock
    with store.transaction() as tx:
        tx.find(row_a)["next_action"] = "email recruiter"
    store.after_read = None

    assert scan["proc"].wait(timeout=20) == 0
    _, rows = store.read_apps()
    by_company = {r["company"]: r for r in rows}
    assert by_company["RowA"]["next_action"] == "email recruiter"
    assert by_company["ScanCo"]["role"] == "Scanned Role"
    assert len(rows) == 2


def test_change_made_between_requests_is_not_lost(store):
    with store.transaction() as tx:
        row_a = tx.add_app({"company": "RowA", "role": "R"})["id"]
    other = Store(store.data_dir, today=store.today)  # a second process's view
    with other.transaction() as tx:
        tx.add_app({"company": "RowB", "role": "R"})
    with store.transaction() as tx:  # the first instance re-reads, so RowB survives
        tx.find(row_a)["notes"] = "x"
    _, rows = store.read_apps()
    assert {r["company"] for r in rows} == {"RowA", "RowB"}
