# Job Tracker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local FastAPI + HTMX app (`jobs` command) that manages the user's job hunt on top of CSV files in a Google Drive vault, replacing the Excel log.

**Architecture:** `storage.py` is the only module that touches data files; every write is a locked read-modify-atomic-replace transaction by `id`. `service.py` holds the actions (each = one transaction + its event), `stats.py` holds pure computations over rows, `routes.py` renders Jinja pages with HTMX partials. `cli.py` starts and stops the server on demand; there is no idle shutdown. The CSVs are the single source of truth: there is no Excel import, and the user's agent creates and edits the CSVs directly under the same lock protocol.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX + Chart.js (CDN), filelock, uvicorn, python-multipart; dev: pytest, httpx; `uv` for env and tool install; `uv_build` as the build backend.

**Spec:** `docs/superpowers/specs/2026-10-11-job-tracker-design.md`

## Global Constraints

- Python 3.12; dependencies exactly: fastapi, jinja2, filelock, uvicorn, python-multipart (runtime); pytest, httpx (dev). Ask the user before adding anything else.
- No React, no build step, no DB server, no ORM.
- CSV is UTF-8 with a header row; dates are ISO `YYYY-MM-DD`.
- Every write: lock `.tracker.lock` in the data dir, re-read from disk, apply by `id`, temp file in the same folder, `fsync`, `os.replace`.
- A CSV that fails to parse is never overwritten; pages show a clear error.
- First write of each day copies the CSVs to `backups/YYYY-MM-DD/`; keep 14 days.
- Unknown CSV columns are preserved untouched.
- `JOBS_DATA_DIR` env var sets the data dir; default `~/Library/CloudStorage/GoogleDrive-binhp8699@gmail.com/My Drive/DriveSyncFiles/Obsidian/Plans/Employment/Job Tracker`. CVs resolve against the parent of the data dir.
- Server listens on `127.0.0.1:8765` (override with `JOBS_PORT`, used by tests). No auto-shutdown, no heartbeat, nothing at boot.
- Never touch the real vault data folder while developing; tests use temp dirs and the walkthrough runs against a copy. There is no Excel import and the app never reads the Excel file.
- Statuses: `Ready, Applied, Assessment, Interview, Final, Offer, Rejected, No response, Withdrawn, Skipped, Incomplete`. Positive = Assessment, Interview, Final, Offer. Submitted = all except Ready, Skipped, Incomplete.
- Every git commit message ends with the trailer `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>` (pass as a second `-m`).
- Run commands from the repo root `/Users/binhphan/projects/application_tracker`. Use `uv run pytest ...`.

## Review Focus

Inputs the spec implies but no feature test covers by itself, most likely first. Each has a test in the task named in brackets.

1. First run: the data dir or CSVs do not exist yet; every page must render empty, not 500. [Task 5]
2. Dates that are blank or non-ISO (agent-written rows may have submitted jobs with no `date_applied`); dashboard, follow-ups and the 30-day rule must treat them as "no date", never crash. [Tasks 3, 4]
3. A scan-written row with an unknown status (e.g. `Pending`) or hostile text (HTML in notes, `javascript:` job link); it must show in All, be ignored by stats, be HTML-escaped, and not render as a link. [Tasks 4, 6]
4. Zero submitted applications; the dashboard must not divide by zero. [Task 4]
5. CSV quirks written by other tools: BOM, CRLF, embedded newlines and quotes in notes, and ragged rows (corrupt). [Task 2]

Known limitation (by spec): "positive" and the funnel use the *current* status, so a job that reached Interview and was later Rejected is not counted as positive. Events could fix this later.

---

### Task 1: Scaffold and `models.py`

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `src/job_tracker/__init__.py`, `src/job_tracker/models.py`, `tests/conftest.py`, `tests/test_models.py`

**Interfaces:**
- Produces (`job_tracker.models`): constants `APP_COLUMNS: list[str]`, `EVENT_COLUMNS: list[str]`, `STATUSES: list[str]`, `POSITIVE: set[str]`, `SUBMITTED: set[str]`, `TERMINAL: set[str]`, `STAGE_BUTTONS: list[str]`, `SKIP_REASONS: list[str]`, `OTHER_REASON: str`, `DEFAULT_SETTINGS: dict`, `EDITABLE_FIELDS: tuple[str, ...]`; functions `parse_date(s) -> date | None`, `row_date(row) -> date | None`, `age_days(row, today) -> int | None`, `normalise_company(name) -> str`, `find_duplicates(rows, company, role, today, exclude_id=None, window_days=60, threshold=0.8) -> list[dict]`, `stage_changed_ids(events) -> set[str]`, `rule_eligible(row, changed_ids, today, days) -> bool`, `tailor_prefix(row) -> str`.

- [ ] **Step 1: Init repo and project files**

```bash
git init
uv python pin 3.12
```

`pyproject.toml`:

```toml
[project]
name = "job-tracker"
version = "0.1.0"
description = "Local job application tracker"
requires-python = ">=3.12"
dependencies = [
    "fastapi",
    "jinja2",
    "filelock",
    "uvicorn",
    "python-multipart",
]

[project.scripts]
jobs = "job_tracker.cli:main"

[dependency-groups]
dev = ["pytest", "httpx"]

[build-system]
requires = ["uv_build>=0.13,<0.14"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
```

`.gitignore`:

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
```

`src/job_tracker/__init__.py`: empty file. Then:

```bash
uv sync
```

Expected: creates `.venv`, installs the packages above.

- [ ] **Step 2: Write `tests/conftest.py`** (shared fixtures; later tasks rely on these)

```python
from datetime import date

import pytest

from job_tracker import service
from job_tracker.storage import Store

TODAY = date(2026, 10, 14)  # a Wednesday; Monday of that week is 2026-10-12


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "Job Tracker", today=lambda: TODAY)


@pytest.fixture
def seed(store):
    """seed(**fields) -> new application id. Defaults: a Ready Backend Engineer at Acme."""

    def _seed(**fields):
        base = {"company": "Acme Pty Ltd", "role": "Backend Engineer", "status": "Ready"}
        return service.add_application(store, {**base, **fields})

    return _seed
```

(`service` and `storage` do not exist yet; tests in this task do not use these fixtures, and pytest only imports `conftest` — so temporarily this import fails. Avoid that by creating empty placeholder modules in Step 3.)

- [ ] **Step 3: Write failing model tests** `tests/test_models.py`

```python
from datetime import date, timedelta

import pytest

from job_tracker.models import (
    find_duplicates,
    normalise_company,
    parse_date,
    rule_eligible,
    stage_changed_ids,
    tailor_prefix,
)

TODAY = date(2026, 10, 14)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Atlassian Pty. Ltd.", "atlassian"),
        ("ANZ Limited", "anz"),
        ("  Canva   PTY LTD ", "canva"),
        ("Smith & Co Pty Ltd", "smith co"),
        ("Pty Ltd", ""),
    ],
)
def test_normalise_company(raw, expected):
    assert normalise_company(raw) == expected


def test_parse_date_tolerates_garbage():
    assert parse_date("2026-10-14") == date(2026, 10, 14)
    assert parse_date("") is None
    assert parse_date("unknown") is None
    assert parse_date(None) is None


def _row(id, company, role, days_ago, field="date_applied"):
    return {"id": id, "company": company, "role": role, field: (TODAY - timedelta(days=days_ago)).isoformat()}


def test_duplicate_matches_normalised_company_and_similar_role():
    rows = [_row("a1", "Acme Pty Ltd", "Backend Engineer", 10)]
    hits = find_duplicates(rows, "ACME Limited", "Backend Engineer (Java)", TODAY)
    assert [r["id"] for r in hits] == ["a1"]


def test_duplicate_role_threshold_is_inclusive():
    from difflib import SequenceMatcher

    rows = [_row("a1", "Acme", "Backend Engineer", 1)]
    role = "Backend Developer"
    ratio = SequenceMatcher(None, role.lower(), "backend engineer").ratio()
    assert find_duplicates(rows, "Acme", role, TODAY, threshold=ratio)
    assert not find_duplicates(rows, "Acme", role, TODAY, threshold=ratio + 0.001)


def test_duplicate_different_role_not_flagged():
    rows = [_row("a1", "Acme", "Marketing Manager", 1)]
    assert find_duplicates(rows, "Acme", "Backend Engineer", TODAY) == []


def test_duplicate_window_is_60_days_inclusive():
    rows = [_row("in", "Acme", "Backend Engineer", 60), _row("out", "Acme", "Backend Engineer", 61)]
    assert [r["id"] for r in find_duplicates(rows, "Acme", "Backend Engineer", TODAY)] == ["in"]


def test_duplicate_falls_back_to_created_at_and_skips_undated_and_self():
    rows = [
        _row("c1", "Acme", "Backend Engineer", 5, field="created_at"),
        {"id": "u1", "company": "Acme", "role": "Backend Engineer"},
        _row("me", "Acme", "Backend Engineer", 1),
    ]
    hits = find_duplicates(rows, "Acme", "Backend Engineer", TODAY, exclude_id="me")
    assert [r["id"] for r in hits] == ["c1"]


def test_rule_eligible_edges():
    def row(age):
        return {"id": "a1", "status": "Applied", "date_applied": (TODAY - timedelta(days=age)).isoformat()}

    assert not rule_eligible(row(29), set(), TODAY, 30)
    assert rule_eligible(row(30), set(), TODAY, 30)
    assert rule_eligible(row(31), set(), TODAY, 30)
    assert not rule_eligible(row(31), {"a1"}, TODAY, 30)  # already moved through a stage
    assert not rule_eligible({"id": "a1", "status": "Applied", "date_applied": ""}, set(), TODAY, 30)
    assert not rule_eligible({**row(40), "status": "Interview"}, set(), TODAY, 30)


def test_stage_changed_ignores_applied_and_ready_events():
    events = [
        {"application_id": "a1", "to_status": "Ready"},
        {"application_id": "a1", "to_status": "Applied"},
        {"application_id": "a2", "to_status": "Interview"},
    ]
    assert stage_changed_ids(events) == {"a2"}


def test_tailor_prefix():
    row = {"id": "a0007", "company": "Acme", "role": "Backend Engineer"}
    assert tailor_prefix(row) == "Tailor my CV for Acme - Backend Engineer (tracker id a0007). Job ad:"
```

- [ ] **Step 4: Create placeholder modules so `conftest` imports**

Create `src/job_tracker/service.py` and `src/job_tracker/storage.py` each containing only `"""placeholder, filled in later tasks."""`. Add to `storage.py` a stub `class Store: ...` so the conftest import succeeds.

- [ ] **Step 5: Run to verify failure**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL (`ImportError: cannot import name 'find_duplicates' from 'job_tracker.models'` or module not found).

- [ ] **Step 6: Write `src/job_tracker/models.py`**

```python
"""Constants and pure helpers shared by every other module. No I/O."""
from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher

APP_COLUMNS = [
    "id", "status", "date_applied", "company", "role", "level", "location",
    "work_mode", "source", "job_link", "job_id", "cv_variant", "cv_file",
    "priority", "match_reason", "next_action", "next_action_date", "contact",
    "salary", "skip_reason", "notes", "created_at", "last_update",
]
EVENT_COLUMNS = ["id", "application_id", "date", "from_status", "to_status", "note"]

STATUSES = [
    "Ready", "Applied", "Assessment", "Interview", "Final", "Offer",
    "Rejected", "No response", "Withdrawn", "Skipped", "Incomplete",
]
POSITIVE = {"Assessment", "Interview", "Final", "Offer"}
SUBMITTED = set(STATUSES) - {"Ready", "Skipped", "Incomplete"}
TERMINAL = {"Rejected", "Withdrawn", "No response", "Skipped", "Incomplete"}
STAGE_BUTTONS = ["Assessment", "Interview", "Final", "Offer", "Rejected", "Withdrawn", "No response"]

OTHER_REASON = "other (type it)"
SKIP_REASONS = [
    "needs citizenship/PR", "security clearance", ".NET/C# required",
    "interstate on-site", "too senior", "too junior", "not software", OTHER_REASON,
]

DEFAULT_SETTINGS = {
    "weekly_target": 8,
    "auto_reject_days": 30,
    "positive_rule": {"per": 40, "min_positive": 4},
}

# Fields the detail page may edit (never id, status, created_at, ...).
EDITABLE_FIELDS = (
    "next_action", "next_action_date", "contact", "salary", "notes",
    "cv_variant", "cv_file", "job_link", "priority", "level", "location",
    "work_mode", "source", "match_reason",
)


def parse_date(s) -> date | None:
    try:
        return date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


def row_date(row: dict) -> date | None:
    """Best date for 'when did this job happen': applied, else created, else last update."""
    return parse_date(row.get("date_applied")) or parse_date(row.get("created_at")) or parse_date(row.get("last_update"))


def age_days(row: dict, today: date) -> int | None:
    applied = parse_date(row.get("date_applied"))
    return None if applied is None else (today - applied).days


_SUFFIX = re.compile(r"\b(pty ltd|pty limited|limited|ltd|pty)\b")


def normalise_company(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", (name or "").lower())
    return " ".join(_SUFFIX.sub(" ", s).split())


def find_duplicates(rows, company, role, today, exclude_id=None, window_days=60, threshold=0.8) -> list[dict]:
    target = normalise_company(company)
    if not target:
        return []
    wanted = (role or "").lower().strip()
    hits = []
    for r in rows:
        if r.get("id") == exclude_id or normalise_company(r.get("company", "")) != target:
            continue
        d = row_date(r)
        if d is None or not 0 <= (today - d).days <= window_days:
            continue
        if SequenceMatcher(None, wanted, r.get("role", "").lower().strip()).ratio() >= threshold:
            hits.append(r)
    return hits


def stage_changed_ids(events: list[dict]) -> set[str]:
    """Applications that have ever moved to a status other than Ready/Applied."""
    return {e["application_id"] for e in events if e.get("to_status") not in ("Applied", "Ready")}


def rule_eligible(row: dict, changed_ids: set[str], today: date, days: int) -> bool:
    age = age_days(row, today)
    return row.get("status") == "Applied" and age is not None and age >= days and row.get("id") not in changed_ids


def tailor_prefix(row: dict) -> str:
    return f"Tailor my CV for {row['company']} - {row['role']} (tracker id {row['id']}). Job ad:"
```

- [ ] **Step 7: Run to verify pass**

Run: `uv run pytest tests/test_models.py -v`
Expected: all PASS. (If the `test_duplicate_role_threshold_is_inclusive` ratio math surprises you, the test computes the ratio itself, so it holds for any value.)

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: project scaffold and models" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `storage.py` (safe writes)

**Files:**
- Modify (replace placeholder): `src/job_tracker/storage.py`
- Test: `tests/test_storage.py`, `tests/test_concurrency.py`

**Interfaces:**
- Consumes: `models.APP_COLUMNS`, `EVENT_COLUMNS`, `DEFAULT_SETTINGS`.
- Produces (`job_tracker.storage`):
  - `data_dir_from_env() -> Path`
  - `class CorruptCsvError(Exception)` with `.path`, `.detail`
  - `class AppNotFound(Exception)`
  - `read_csv(path, default_header, required=("id",)) -> tuple[list[str], list[dict]]`
  - `write_csv(path, header, rows) -> None` (atomic)
  - `class Tx` with attrs `app_header`, `apps`, `ev_header`, `events`, `today: str` and methods `find(app_id) -> dict` (raises `AppNotFound`), `add_app(fields: dict) -> dict`, `add_event(app_id, from_status, to_status, note="") -> dict`
  - `class Store(data_dir: Path, today=date.today)` with attrs `data_dir`, `apps_path`, `events_path`, `lock_path`, `today`, `after_read` (test hook, default `None`) and methods `transaction()` (context manager yielding `Tx`), `locked()` (context manager, holds the lock only), `backup_once()`, `read_apps() -> (header, rows)`, `read_events() -> (header, rows)`, `get_app(app_id) -> dict`, `settings() -> dict`, `read_ad(app_id) -> str`, `write_ad(app_id, text)`, `write_file(name, text)`.

- [ ] **Step 1: Write failing tests** `tests/test_storage.py`

```python
import csv
from datetime import date

import pytest

from job_tracker.storage import AppNotFound, CorruptCsvError, Store, read_csv


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


def test_exception_inside_transaction_writes_nothing(store, seed):
    seed()
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


def test_no_temp_files_left_behind(store, seed):
    seed()
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
```

`tests/test_concurrency.py`:

```python
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


def test_external_append_between_read_and_write_survives(store, seed):
    row_a = seed(company="RowA")

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


def test_change_made_between_requests_is_not_lost(store, seed):
    row_a = seed(company="RowA")
    other = Store(store.data_dir, today=store.today)  # a second process's view
    with other.transaction() as tx:
        tx.add_app({"company": "RowB", "role": "R"})
    with store.transaction() as tx:  # the first instance re-reads, so RowB survives
        tx.find(row_a)["notes"] = "x"
    _, rows = store.read_apps()
    assert {r["company"] for r in rows} == {"RowA", "RowB"}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_storage.py tests/test_concurrency.py -v`
Expected: FAIL (stub `Store` has no `transaction`).

- [ ] **Step 3: Implement `src/job_tracker/storage.py`**

```python
"""All file access. Every write: lock, re-read, apply by id, temp file, fsync, os.replace."""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from pathlib import Path

from filelock import FileLock

from .models import APP_COLUMNS, DEFAULT_SETTINGS, EVENT_COLUMNS

DEFAULT_DATA_DIR = Path.home() / (
    "Library/CloudStorage/GoogleDrive-binhp8699@gmail.com/My Drive/"
    "DriveSyncFiles/Obsidian/Plans/Employment/Job Tracker"
)
BACKUP_DAYS = 14
_DATE_DIR = re.compile(r"\d{4}-\d{2}-\d{2}")
_SAFE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def data_dir_from_env() -> Path:
    return Path(os.environ.get("JOBS_DATA_DIR") or DEFAULT_DATA_DIR).expanduser()


class CorruptCsvError(Exception):
    """A data file could not be parsed. The file is left untouched."""

    def __init__(self, path: Path, detail: str):
        super().__init__(f"{path.name} could not be read: {detail}")
        self.path = path
        self.detail = detail


class AppNotFound(Exception):
    pass


def read_csv(path: Path, default_header: list[str], required=("id",)) -> tuple[list[str], list[dict]]:
    if not path.exists() or path.stat().st_size == 0:
        return list(default_header), []
    try:
        with path.open(newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f, strict=True)
            header = reader.fieldnames or []
            if not header or len(set(header)) != len(header):
                raise CorruptCsvError(path, "missing or duplicate header")
            for col in required:
                if col not in header:
                    raise CorruptCsvError(path, f"no '{col}' column")
            rows = []
            for line in reader:
                if None in line:
                    raise CorruptCsvError(path, f"line {reader.line_num} has more fields than the header")
                if None in line.values():
                    raise CorruptCsvError(path, f"line {reader.line_num} has fewer fields than the header")
                rows.append(line)
    except (csv.Error, UnicodeDecodeError) as e:
        raise CorruptCsvError(path, str(e)) from e
    return list(header), rows


def _atomic_write(path: Path, write) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            write(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_csv(path: Path, header: list[str], rows: list[dict]) -> None:
    def write(f):
        w = csv.DictWriter(f, fieldnames=header, restval="", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)

    _atomic_write(path, write)


def _with_known_columns(header: list[str], rows: list[dict], known: list[str]) -> list[str]:
    header = header + [c for c in known if c not in header]
    for r in rows:
        for c in header:
            r.setdefault(c, "")
    return header


def _next_id(prefix: str, rows: list[dict]) -> str:
    nums = [int(m.group(1)) for r in rows if (m := re.fullmatch(prefix + r"(\d+)", r.get("id", "")))]
    return f"{prefix}{max(nums, default=0) + 1:04d}"


class Tx:
    """A loaded snapshot, valid only while the lock is held."""

    def __init__(self, app_header, apps, ev_header, events, today: str):
        self.app_header, self.apps = app_header, apps
        self.ev_header, self.events = ev_header, events
        self.today = today

    def find(self, app_id: str) -> dict:
        for r in self.apps:
            if r["id"] == app_id:
                return r
        raise AppNotFound(app_id)

    def add_app(self, fields: dict) -> dict:
        for k in fields:
            if k not in self.app_header:
                self.app_header.append(k)
        row = {c: "" for c in self.app_header}
        row.update({k: v for k, v in fields.items() if k != "id"})
        row["id"] = _next_id("a", self.apps)
        self.apps.append(row)
        return row

    def add_event(self, app_id: str, from_status: str, to_status: str, note: str = "") -> dict:
        ev = {
            "id": _next_id("e", self.events), "application_id": app_id, "date": self.today,
            "from_status": from_status, "to_status": to_status, "note": note,
        }
        self.events.append(ev)
        return ev


class Store:
    def __init__(self, data_dir: Path, today=date.today):
        self.data_dir = Path(data_dir)
        self.today = today
        self.after_read = None  # test hook: called after the re-read, while the lock is held
        self.apps_path = self.data_dir / "applications.csv"
        self.events_path = self.data_dir / "events.csv"
        self.lock_path = self.data_dir / ".tracker.lock"

    # ---- reads (no lock needed: writers replace files atomically) ----
    def read_apps(self) -> tuple[list[str], list[dict]]:
        header, rows = read_csv(self.apps_path, APP_COLUMNS)
        return _with_known_columns(header, rows, APP_COLUMNS), rows

    def read_events(self) -> tuple[list[str], list[dict]]:
        header, rows = read_csv(self.events_path, EVENT_COLUMNS)
        return _with_known_columns(header, rows, EVENT_COLUMNS), rows

    def get_app(self, app_id: str) -> dict:
        for r in self.read_apps()[1]:
            if r["id"] == app_id:
                return r
        raise AppNotFound(app_id)

    def settings(self) -> dict:
        s = deepcopy(DEFAULT_SETTINGS)
        path = self.data_dir / "settings.json"
        if path.exists():
            try:
                user = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(user, dict):
                    raise ValueError("expected a JSON object")
            except ValueError as e:
                raise CorruptCsvError(path, str(e)) from e
            rule = user.pop("positive_rule", {})
            s.update(user)
            if isinstance(rule, dict):
                s["positive_rule"].update(rule)
        return s

    def _ad_path(self, app_id: str) -> Path:
        if not _SAFE_NAME.fullmatch(app_id):
            raise ValueError("bad application id")
        return self.data_dir / "job_ads" / f"{app_id}.md"

    def read_ad(self, app_id: str) -> str:
        path = self._ad_path(app_id)
        return path.read_text(encoding="utf-8", newline="") if path.exists() else ""

    # ---- writes ----
    @contextmanager
    def locked(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.lock_path)):
            yield

    def write_ad(self, app_id: str, text: str) -> None:
        path = self._ad_path(app_id)
        with self.locked():
            path.parent.mkdir(exist_ok=True)
            _atomic_write(path, lambda f: f.write(text))

    def write_file(self, name: str, text: str) -> None:
        with self.locked():
            _atomic_write(self.data_dir / name, lambda f: f.write(text))

    def backup_once(self) -> None:
        """Copy the CSVs to backups/<today>/ the first time we write each day. Lock must be held."""
        day = self.data_dir / "backups" / self.today().isoformat()
        if day.exists():
            return
        day.mkdir(parents=True)
        for path in (self.apps_path, self.events_path):
            if path.exists():
                shutil.copy2(path, day / path.name)
        dated = sorted(d for d in day.parent.iterdir() if d.is_dir() and _DATE_DIR.fullmatch(d.name))
        for old in dated[:-BACKUP_DAYS]:
            shutil.rmtree(old)

    @contextmanager
    def transaction(self):
        with self.locked():
            app_header, apps = self.read_apps()
            ev_header, events = self.read_events()
            tx = Tx(app_header, apps, ev_header, events, self.today().isoformat())
            if self.after_read:
                self.after_read()
            yield tx  # an exception in the body skips everything below
            self.backup_once()
            write_csv(self.apps_path, tx.app_header, tx.apps)
            write_csv(self.events_path, tx.ev_header, tx.events)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_storage.py tests/test_concurrency.py -v`
Expected: all PASS. Note: `test_daily_backup...` loops `range(3, 21)`; days 1 to 20 give 20 backup dirs, pruned to the latest 14 (09-07 to 09-20).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: locked atomic CSV storage with backups" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: `service.py` (actions)

**Files:**
- Modify (replace placeholder): `src/job_tracker/service.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: `Store`, `Tx`, `AppNotFound` (Task 2); `models` constants and `rule_eligible`, `stage_changed_ids`, `parse_date`; `stats.skip_tally` (Task 4). To avoid a circular dependency, `export_skip_reasons` is implemented in Task 4 Step 6 inside `service.py` after `stats.py` exists. Until then it is absent.
- Produces (`job_tracker.service`), all take `store` first:
  - `add_application(store, fields: dict) -> str` (new id; `ValueError` if company or role blank or status invalid)
  - `change_status(store, app_id, new_status, note="") -> None`
  - `mark_applied(store, app_id, ad_text="") -> None`
  - `skip(store, app_id, reason) -> None`
  - `update_details(store, app_id, fields: dict) -> None`
  - `save_ad(store, app_id, text) -> None`
  - `apply_rejection_rule(store) -> list[str]` (ids rejected)

- [ ] **Step 1: Write failing tests** `tests/test_service.py`

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_service.py -v`
Expected: FAIL (`AttributeError: module 'job_tracker.service' has no attribute ...`).

- [ ] **Step 3: Implement `src/job_tracker/service.py`**

```python
"""Actions. Each is one locked transaction that also appends its event."""
from __future__ import annotations

from datetime import date

from .models import APP_COLUMNS, EDITABLE_FIELDS, STATUSES, SUBMITTED, rule_eligible, stage_changed_ids
from .storage import Store, Tx


def _set_status(tx: Tx, row: dict, new_status: str, note: str = "") -> None:
    old = row.get("status", "")
    row["status"] = new_status
    row["last_update"] = tx.today
    tx.add_event(row["id"], old, new_status, note)


def add_application(store: Store, fields: dict) -> str:
    clean = {k: str(v).strip() for k, v in fields.items() if k in APP_COLUMNS and k != "id"}
    if not clean.get("company") or not clean.get("role"):
        raise ValueError("company and role are required")
    status = clean.get("status") or "Ready"
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    with store.transaction() as tx:
        row = tx.add_app({**clean, "status": status, "created_at": tx.today, "last_update": tx.today})
        if status in SUBMITTED and not row["date_applied"]:
            row["date_applied"] = tx.today
        tx.add_event(row["id"], "", status, "added")
        return row["id"]


def change_status(store: Store, app_id: str, new_status: str, note: str = "") -> None:
    if new_status not in STATUSES:
        raise ValueError(f"unknown status: {new_status}")
    with store.transaction() as tx:
        _set_status(tx, tx.find(app_id), new_status, note.strip())


def mark_applied(store: Store, app_id: str, ad_text: str = "") -> None:
    with store.transaction() as tx:
        row = tx.find(app_id)
        _set_status(tx, row, "Applied")
        row["date_applied"] = tx.today
    if ad_text.strip():  # after the transaction: the lock is not re-entrant across instances
        store.write_ad(app_id, ad_text)


def skip(store: Store, app_id: str, reason: str) -> None:
    reason = reason.strip()
    if not reason:
        raise ValueError("a skip reason is required")
    with store.transaction() as tx:
        row = tx.find(app_id)
        _set_status(tx, row, "Skipped", reason)
        row["skip_reason"] = reason


def update_details(store: Store, app_id: str, fields: dict) -> None:
    clean = {k: (fields[k] or "").strip() for k in EDITABLE_FIELDS if k in fields}
    if clean.get("next_action_date"):
        try:
            date.fromisoformat(clean["next_action_date"])
        except ValueError:
            raise ValueError("next_action_date must be YYYY-MM-DD") from None
    with store.transaction() as tx:
        row = tx.find(app_id)
        row.update(clean)
        row["last_update"] = tx.today


def save_ad(store: Store, app_id: str, text: str) -> None:
    store.get_app(app_id)  # raises AppNotFound
    store.write_ad(app_id, text)


def apply_rejection_rule(store: Store) -> list[str]:
    days = store.settings()["auto_reject_days"]
    with store.transaction() as tx:
        today = date.fromisoformat(tx.today)
        changed = stage_changed_ids(tx.events)
        rejected = []
        for row in tx.apps:
            if rule_eligible(row, changed, today, days):
                _set_status(tx, row, "Rejected", f"auto: no reply after {days} days")
                rejected.append(row["id"])
        return rejected
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_service.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "feat: service actions (apply, skip, status, 30-day rule)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `stats.py` (dashboard, follow-ups, skip tally, filtering)

**Files:**
- Create: `src/job_tracker/stats.py`
- Modify: `src/job_tracker/service.py` (append `export_skip_reasons`)
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `models` (`POSITIVE`, `SUBMITTED`, `TERMINAL`, `parse_date`, `row_date`, `age_days`, `stage_changed_ids`, `rule_eligible`), `Store.write_file`, `Store.read_apps`.
- Produces (`job_tracker.stats`):
  - `role_type(title) -> str`
  - `dashboard(rows, settings, today) -> dict` with keys: `week_applied:int`, `week_target:int`, `submitted:int`, `positive:int`, `rate: float|None`, `rule: "too_early"|"cv_problem"|"ok"`, `rule_per:int`, `rule_min:int`, `weekly: list[{"week": iso, "count": int}]` (12, oldest first), `funnel: {"submitted","positive","interview","offer"}`, `by_cv`, `by_source`, `by_role` (each `list[{"key","submitted","positive","rate"}]`), `ready:int`, `overdue:int`
  - `followups(rows, events, settings, today) -> dict` with keys `overdue`, `due_week`, `about_to_reject`, `rule_candidates` (lists of row dicts)
  - `skip_tally(rows, today, days=30, max_examples=3) -> list[{"reason","count","examples":[{"company","role"}]}]`
  - `filter_rows(rows, q="", status="", source="", cv_variant="", date_from="", date_to="", sort="last_update", direction="desc") -> list[dict]`; `SORTABLE: tuple[str,...]`
- `service.export_skip_reasons(store) -> Path` (writes `skip_reasons.json`).

- [ ] **Step 1: Write failing tests** `tests/test_stats.py`

Hand-computed fixture (today = Wed 2026-10-14, week starts Mon 2026-10-12; settings per=5, min_positive=1, target=8):

| id | status | date_applied | cv_variant | source | role | next_action_date |
|---|---|---|---|---|---|---|
| a1 | Applied | 10-12 | Java backend | Seek | Backend Engineer | |
| a2 | Applied | 10-13 | Java backend | LinkedIn | Platform Engineer | 10-10 |
| a3 | Interview | 10-05 | Platform | Seek | DevOps Engineer | |
| a4 | Rejected | 10-06 | Java backend | Seek | Backend Developer | 10-01 |
| a5 | Offer | 09-29 | Platform | LinkedIn | Full Stack Developer | |
| a6 | Ready | (created 10-10) | Original | Seek | Backend Engineer | |
| a7 | Skipped | | | | Data Analyst | |
| a8 | Assessment | 10-12 | Grad | Seek | Graduate Engineer | |

Expected: submitted = 6 (a1,a2,a3,a4,a5,a8); positive = 3 (a3,a5,a8), rate 0.5; this-week applied = 3 (a1,a2,a8); rule: 6 ≥ 5 and 3·5 ≥ 1·6 so `ok`; weekly counts: week 10-12 → 3, week 10-05 → 2 (a3,a4), week 09-28 → 1 (a5), the rest 0; funnel 6/3/interview 2 (a3,a5)/offer 1; by_cv: Java backend 3/0, Platform 2/2, Grad 1/1; by_source: Seek 4/2, LinkedIn 2/1; by_role: backend 2/0, platform 2/1, full-stack 1/1, graduate 1/1; ready 1; overdue 1 (a2; a4 is Rejected so excluded).

```python
from datetime import date, timedelta

import pytest

from job_tracker import service, stats
from conftest import TODAY

SETTINGS = {"weekly_target": 8, "auto_reject_days": 30, "positive_rule": {"per": 5, "min_positive": 1}}


def r(id, status, date_applied="", cv="", source="", role="Backend Engineer", nad="", **extra):
    base = {c: "" for c in ["id", "status", "date_applied", "cv_variant", "source", "role", "company",
                            "next_action_date", "created_at", "last_update", "notes", "skip_reason", "priority"]}
    base.update(id=id, status=status, date_applied=date_applied, cv_variant=cv, source=source, role=role,
                company=f"Co {id}", next_action_date=nad)
    base.update(extra)
    return base


FIXTURE = [
    r("a1", "Applied", "2026-10-12", "Java backend", "Seek", "Backend Engineer"),
    r("a2", "Applied", "2026-10-13", "Java backend", "LinkedIn", "Platform Engineer", nad="2026-10-10"),
    r("a3", "Interview", "2026-10-05", "Platform", "Seek", "DevOps Engineer"),
    r("a4", "Rejected", "2026-10-06", "Java backend", "Seek", "Backend Developer", nad="2026-10-01"),
    r("a5", "Offer", "2026-09-29", "Platform", "LinkedIn", "Full Stack Developer"),
    r("a6", "Ready", cv="Original", source="Seek", created_at="2026-10-10"),
    r("a7", "Skipped", role="Data Analyst"),
    r("a8", "Assessment", "2026-10-12", "Grad", "Seek", "Graduate Engineer"),
]


def by_key(rows):
    return {x["key"]: (x["submitted"], x["positive"]) for x in rows}


def test_dashboard_numbers():
    d = stats.dashboard(FIXTURE, SETTINGS, TODAY)
    assert (d["submitted"], d["positive"], d["rate"]) == (6, 3, pytest.approx(0.5))
    assert (d["week_applied"], d["week_target"]) == (3, 8)
    assert d["rule"] == "ok"
    assert d["funnel"] == {"submitted": 6, "positive": 3, "interview": 2, "offer": 1}
    assert (d["ready"], d["overdue"]) == (1, 1)
    assert len(d["weekly"]) == 12 and d["weekly"][-1] == {"week": "2026-10-12", "count": 3}
    counts = {w["week"]: w["count"] for w in d["weekly"]}
    assert counts["2026-10-05"] == 2 and counts["2026-09-28"] == 1
    assert sum(counts.values()) == 6
    assert by_key(d["by_cv"]) == {"Java backend": (3, 0), "Platform": (2, 2), "Grad": (1, 1)}
    assert by_key(d["by_source"]) == {"Seek": (4, 2), "LinkedIn": (2, 1)}
    assert by_key(d["by_role"]) == {"backend": (2, 0), "platform/devops/cloud": (2, 1), "full-stack": (1, 1), "graduate": (1, 1)}
    assert d["by_cv"][0]["key"] == "Java backend"  # sorted by submitted desc
    assert {x["key"]: x["rate"] for x in d["by_cv"]}["Platform"] == pytest.approx(1.0)


def test_rule_check_states():
    few = [r(f"a{i}", "Applied", "2026-10-01") for i in range(4)]
    assert stats.dashboard(few, SETTINGS, TODAY)["rule"] == "too_early"  # 4 < per=5
    bad = [r(f"a{i}", "Applied", "2026-10-01") for i in range(10)]  # 0 positive in 10: 0*5 < 1*10
    assert stats.dashboard(bad, SETTINGS, TODAY)["rule"] == "cv_problem"
    edge = bad[:-2] + [r("p1", "Interview", "2026-10-01"), r("p2", "Interview", "2026-10-01")]
    assert stats.dashboard(edge, SETTINGS, TODAY)["rule"] == "ok"  # 2*5=10 >= 1*10


def test_dashboard_empty_and_hostile_rows():
    d = stats.dashboard([], SETTINGS, TODAY)
    assert d["submitted"] == 0 and d["rate"] is None and d["rule"] == "too_early"
    assert d["by_cv"] == [] and len(d["weekly"]) == 12
    weird = [r("a1", "Pending", "2026-10-12"), r("a2", "Applied", "unknown"), r("a3", "Applied", "")]
    d = stats.dashboard(weird, SETTINGS, TODAY)
    assert d["submitted"] == 2 and d["week_applied"] == 0 and sum(w["count"] for w in d["weekly"]) == 0
    assert by_key(d["by_cv"]) == {"(none)": (2, 0)}


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Graduate Software Engineer", "graduate"),
        ("Senior Backend Engineer (Java)", "backend"),
        ("DevOps Engineer", "platform/devops/cloud"),
        ("Cloud Platform Engineer", "platform/devops/cloud"),
        ("Full Stack Developer", "full-stack"),
        ("Full-Stack AI Developer", "AI/ML"),
        ("ML Engineer", "AI/ML"),
        ("Product Manager", "other"),
        ("", "other"),
    ],
)
def test_role_type(title, expected):
    assert stats.role_type(title) == expected


def test_followups_lists():
    rows = [
        r("o1", "Applied", "2026-10-01", nad="2026-10-10"),       # overdue
        r("o2", "Interview", "2026-10-01", nad="2026-10-13"),     # overdue
        r("t0", "Applied", "2026-10-01", nad="2026-10-14"),       # due today
        r("t7", "Applied", "2026-10-01", nad="2026-10-21"),       # due in 7 days (inclusive)
        r("t8", "Applied", "2026-10-01", nad="2026-10-22"),       # beyond the week
        r("x1", "Rejected", "2026-10-01", nad="2026-10-01"),      # terminal: ignored
        r("x2", "Applied", "2026-10-01", nad="soon"),             # bad date: ignored
        r("g25", "Applied", "2026-09-19"),                         # 25 days: about to reject
        r("g29", "Applied", "2026-09-15"),                         # 29 days
        r("g30", "Applied", "2026-09-14"),                         # 30 days: rule candidate, not "about to"
        r("g24", "Applied", "2026-09-20"),                         # 24 days: too early
    ]
    f = stats.followups(rows, [], SETTINGS, TODAY)
    assert [x["id"] for x in f["overdue"]] == ["o1", "o2"]
    assert [x["id"] for x in f["due_week"]] == ["t0", "t7"]
    assert [x["id"] for x in f["about_to_reject"]] == ["g29", "g25"]  # oldest first
    assert [x["id"] for x in f["rule_candidates"]] == ["g30"]


def test_skip_tally_window_and_examples():
    rows = [
        r("s1", "Skipped", skip_reason="too senior", last_update="2026-10-10", company="A", role="X"),
        r("s2", "Skipped", skip_reason="too senior", last_update="2026-09-14", company="B", role="Y"),  # 30 days: in
        r("s3", "Skipped", skip_reason="too senior", last_update="2026-09-13", company="C", role="Z"),  # 31 days: out
        r("s4", "Skipped", skip_reason="Needs Go", last_update="2026-10-01", company="D", role="W"),
        r("s5", "Skipped", skip_reason="", last_update="2026-10-01"),
        r("s6", "Applied", skip_reason="stale", last_update="2026-10-01"),
    ]
    tally = stats.skip_tally(rows, TODAY)
    assert [(t["reason"], t["count"]) for t in tally] == [("too senior", 2), ("Needs Go", 1)]
    assert tally[0]["examples"] == [{"company": "A", "role": "X"}, {"company": "B", "role": "Y"}]


def test_export_skip_reasons_writes_json(store, seed):
    import json

    app_id = seed(company="Acme", role="Dev")
    service.skip(store, app_id, "too junior")
    path = service.export_skip_reasons(store)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "skip_reasons.json" and data["window_days"] == 30
    assert data["reasons"] == [{"reason": "too junior", "count": 1, "examples": [{"company": "Acme", "role": "Dev"}]}]


def test_filter_rows():
    rows = [
        r("a1", "Applied", "2026-10-01", "Java backend", "Seek", "Backend Engineer", company="Acme", notes="great team"),
        r("a2", "Rejected", "2026-09-01", "Platform", "LinkedIn", "DevOps", company="Beta"),
        r("a3", "Ready", cv="Platform", source="Seek", role="SRE", company="Gamma", created_at="2026-10-10"),
    ]
    ids = lambda **kw: [x["id"] for x in stats.filter_rows(rows, **kw)]
    assert sorted(ids()) == ["a1", "a2", "a3"]
    assert ids(q="GREAT") == ["a1"]                       # searches notes, case-insensitive
    assert ids(q="gamma") == ["a3"]
    assert sorted(ids(status="Applied")) == ["a1"]
    assert sorted(ids(source="Seek")) == ["a1", "a3"]
    assert sorted(ids(cv_variant="Platform")) == ["a2", "a3"]
    assert ids(date_from="2026-09-15", date_to="2026-10-05") == ["a1"]
    assert ids(date_from="2026-10-10") == ["a3"]           # falls back to created_at
    assert ids(sort="company", direction="asc") == ["a1", "a2", "a3"]
    assert ids(sort="company", direction="desc") == ["a3", "a2", "a1"]
    assert ids(sort="nonsense") is not None                # unknown sort column falls back, never crashes
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stats.py -v`
Expected: FAIL (`ModuleNotFoundError` / missing attributes).

- [ ] **Step 3: Implement `src/job_tracker/stats.py`**

```python
"""Pure computations over rows. No file access."""
from __future__ import annotations

import re
from datetime import date, timedelta

from .models import POSITIVE, SUBMITTED, TERMINAL, parse_date, row_date, rule_eligible, stage_changed_ids

SORTABLE = ("company", "role", "status", "source", "cv_variant", "date_applied", "last_update")

_ROLE_RULES = [
    ("graduate", r"\bgraduate\b|\bgrad\b|\bentry[- ]level\b"),
    ("AI/ML", r"\bai\b|\bml\b|machine learning|\bllm\b|data scien"),
    ("platform/devops/cloud", r"devops|platform|cloud|\bsre\b|site reliability|infrastructure"),
    ("full-stack", r"full[- ]?stack"),
    ("backend", r"back[- ]?end|\bjava\b|\bpython\b|\bgolang\b|\bapi\b|\bserver"),
]


def role_type(title: str) -> str:
    t = (title or "").lower()
    for name, pattern in _ROLE_RULES:
        if re.search(pattern, t):
            return name
    return "other"


def _monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _breakdown(rows: list[dict], key_of) -> list[dict]:
    groups: dict[str, list[int]] = {}
    for r in rows:
        if r.get("status") in SUBMITTED:
            g = groups.setdefault(key_of(r) or "(none)", [0, 0])
            g[0] += 1
            g[1] += r["status"] in POSITIVE
    out = [{"key": k, "submitted": s, "positive": p, "rate": p / s} for k, (s, p) in groups.items()]
    return sorted(out, key=lambda x: (-x["submitted"], x["key"]))


def _active_with_date(rows):
    for r in rows:
        d = parse_date(r.get("next_action_date"))
        if d is not None and r.get("status") not in TERMINAL:
            yield d, r


def dashboard(rows: list[dict], settings: dict, today: date) -> dict:
    submitted = [r for r in rows if r.get("status") in SUBMITTED]
    positive = [r for r in submitted if r["status"] in POSITIVE]
    monday = _monday(today)
    applied_dates = [d for d in (parse_date(r.get("date_applied")) for r in submitted) if d]

    starts = [monday - timedelta(weeks=i) for i in range(11, -1, -1)]
    weekly = [{"week": s.isoformat(), "count": sum(s <= d < s + timedelta(days=7) for d in applied_dates)} for s in starts]

    per, minimum = settings["positive_rule"]["per"], settings["positive_rule"]["min_positive"]
    if len(submitted) < per:
        rule = "too_early"
    elif len(positive) * per < minimum * len(submitted):
        rule = "cv_problem"
    else:
        rule = "ok"

    return {
        "week_applied": sum(monday <= d <= today for d in applied_dates),
        "week_target": settings["weekly_target"],
        "submitted": len(submitted),
        "positive": len(positive),
        "rate": len(positive) / len(submitted) if submitted else None,
        "rule": rule, "rule_per": per, "rule_min": minimum,
        "weekly": weekly,
        "funnel": {
            "submitted": len(submitted), "positive": len(positive),
            "interview": sum(r["status"] in ("Interview", "Final", "Offer") for r in submitted),
            "offer": sum(r["status"] == "Offer" for r in submitted),
        },
        "by_cv": _breakdown(rows, lambda r: r.get("cv_variant", "").strip()),
        "by_source": _breakdown(rows, lambda r: r.get("source", "").strip()),
        "by_role": _breakdown(rows, lambda r: role_type(r.get("role", ""))),
        "ready": sum(r.get("status") == "Ready" for r in rows),
        "overdue": sum(d < today for d, _ in _active_with_date(rows)),
    }


def followups(rows: list[dict], events: list[dict], settings: dict, today: date) -> dict:
    days = settings["auto_reject_days"]
    dated = sorted(_active_with_date(rows), key=lambda t: (t[0], t[1]["id"]))
    changed = stage_changed_ids(events)
    applied = [(r, (today - parse_date(r["date_applied"])).days) for r in rows
               if r.get("status") == "Applied" and parse_date(r.get("date_applied"))]
    return {
        "overdue": [r for d, r in dated if d < today],
        "due_week": [r for d, r in dated if today <= d <= today + timedelta(days=7)],
        "about_to_reject": [r for r, age in sorted(applied, key=lambda t: -t[1]) if days - 5 <= age < days],
        "rule_candidates": [r for r in rows if rule_eligible(r, changed, today, days)],
    }


def skip_tally(rows: list[dict], today: date, days: int = 30, max_examples: int = 3) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        reason = (r.get("skip_reason") or "").strip()
        d = parse_date(r.get("last_update"))
        if r.get("status") == "Skipped" and reason and d and (today - d).days <= days:
            groups.setdefault(reason, []).append(r)
    out = [
        {"reason": k, "count": len(v), "examples": [{"company": x["company"], "role": x["role"]} for x in v[:max_examples]]}
        for k, v in groups.items()
    ]
    return sorted(out, key=lambda g: (-g["count"], g["reason"]))


def filter_rows(rows, q="", status="", source="", cv_variant="", date_from="", date_to="",
                sort="last_update", direction="desc") -> list[dict]:
    needle = q.strip().lower()
    start, end = parse_date(date_from), parse_date(date_to)
    out = []
    for r in rows:
        if needle and needle not in " ".join(r.get(k, "") for k in ("company", "role", "notes")).lower():
            continue
        if status and r.get("status") != status:
            continue
        if source and r.get("source") != source:
            continue
        if cv_variant and r.get("cv_variant") != cv_variant:
            continue
        if start or end:
            d = row_date(r)
            if d is None or (start and d < start) or (end and d > end):
                continue
        out.append(r)
    column = sort if sort in SORTABLE else "last_update"
    # blanks always sort last regardless of direction
    filled = sorted((r for r in out if r.get(column)), key=lambda r: r[column].lower(), reverse=direction == "desc")
    return filled + [r for r in out if not r.get(column)]
```

- [ ] **Step 4: Append `export_skip_reasons` to `service.py`**

Add at the top of `service.py`: `import json`, `from pathlib import Path`, `from . import stats`. Then append:

```python
def export_skip_reasons(store: Store, days: int = 30) -> Path:
    """Write skip_reasons.json so the scan can tighten its rules."""
    _, rows = store.read_apps()
    payload = {
        "generated": store.today().isoformat(),
        "window_days": days,
        "reasons": stats.skip_tally(rows, store.today(), days),
    }
    store.write_file("skip_reasons.json", json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return store.data_dir / "skip_reasons.json"
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_stats.py tests/test_service.py -v`
Expected: all PASS. If a hand-computed expectation fails, recheck the fixture table above before touching the code: the table is the source of truth.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: dashboard, follow-up, skip tally and filter computations" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Web skeleton and Queue page (features 1 and 2)

**Files:**
- Create: `src/job_tracker/server.py`, `src/job_tracker/routes.py`, `src/job_tracker/static/app.css`, `src/job_tracker/templates/{base,queue,_card,error}.html`
- Test: `tests/test_routes_queue.py`; extend `tests/conftest.py`

**Interfaces:**
- Consumes: `Store`, `service`, `stats`, `models`.
- Produces: `server.create_app(data_dir: Path | None = None, today=date.today) -> FastAPI` (sets `app.state.store`, `app.state.templates`); `routes.router`; helpers `routes.render(request, name, status_code=200, **ctx)`, `routes.reply(request, fragment, redirect_to)`; `routes.open_file(path)` (monkeypatched in tests); routes `GET /health`, `GET /`, `POST /a/{id}/apply`, `POST /a/{id}/skip`, `POST /a/{id}/open-cv`, `POST /skip-reasons/export`; exception handlers (`CorruptCsvError` → 500 error page, `AppNotFound` → 404, `ValueError` → 400). Templates define blocks `title` and `content`; context var `active` is one of `queue|followups|all|dashboard|""`.

- [ ] **Step 1: Extend `tests/conftest.py`**

Append:

```python
from fastapi.testclient import TestClient

from job_tracker.server import create_app

HX = {"HX-Request": "true"}


@pytest.fixture
def client(store):
    return TestClient(create_app(store.data_dir, today=store.today))
```

- [ ] **Step 2: Write failing tests** `tests/test_routes_queue.py`

```python
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
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_routes_queue.py -v`
Expected: FAIL (`ModuleNotFoundError: job_tracker.server`).

- [ ] **Step 4: Implement `server.py`**

```python
"""App factory and uvicorn entry point (python -m job_tracker.server)."""
from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import routes
from .storage import AppNotFound, CorruptCsvError, Store, data_dir_from_env

BASE = Path(__file__).parent


def create_app(data_dir: Path | None = None, today=date.today) -> FastAPI:
    app = FastAPI(title="Job Tracker")
    app.state.store = Store(data_dir or data_dir_from_env(), today=today)
    templates = Jinja2Templates(directory=BASE / "templates")
    templates.env.filters["pct"] = lambda v: "n/a" if v is None else f"{v * 100:.0f}%"
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(routes.router)

    @app.exception_handler(CorruptCsvError)
    async def corrupt(request, exc):
        return routes.render(request, "error.html", 500, title="A data file could not be read", message=str(exc),
                             hint="The file was not changed. Fix it or restore a copy from the backups folder, then reload.")

    @app.exception_handler(AppNotFound)
    async def not_found(request, exc):
        return routes.render(request, "error.html", 404, title="Application not found", message=str(exc), hint="")

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        return routes.render(request, "error.html", 400, title="That didn't work", message=str(exc), hint="Go back and try again.")

    return app


def main(argv=None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("JOBS_PORT", "8765")))
    args = parser.parse_args(argv)
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
```

- [ ] **Step 5: Implement `routes.py` (queue part; later tasks append to this file)**

```python
"""HTTP routes. Pages are plain Jinja; buttons use HTMX and fall back to normal form posts."""
from __future__ import annotations

import html
import subprocess
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import service, stats
from .models import OTHER_REASON, SKIP_REASONS

router = APIRouter()
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def open_file(path: Path) -> None:
    subprocess.run(["open", str(path)], check=True)


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("active", "")
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)


def reply(request: Request, fragment: str, redirect_to: str):
    """HTMX gets a fragment to swap in; a plain form post gets a redirect."""
    if request.headers.get("hx-request"):
        return HTMLResponse(fragment)
    return RedirectResponse(redirect_to, status_code=303)


def _store(request: Request):
    return request.app.state.store


@router.get("/health")
def health():
    return {"app": "job-tracker", "ok": True}


@router.get("/")
def queue(request: Request):
    store = _store(request)
    _, rows = store.read_apps()
    ready = sorted((r for r in rows if r["status"] == "Ready"), key=lambda r: (r["created_at"], r["id"]), reverse=True)
    groups: dict[str, list[dict]] = {}
    for r in ready:
        groups.setdefault(r["priority"].strip() or "Unprioritised", []).append(r)
    ordered = sorted(groups.items(), key=lambda kv: (PRIORITY_RANK.get(kv[0].lower(), 3), kv[0]))
    return render(request, "queue.html", active="queue", groups=ordered, total=len(ready),
                  tally=stats.skip_tally(rows, store.today()), skip_reasons=SKIP_REASONS)


@router.post("/a/{app_id}/apply")
def apply(request: Request, app_id: str, ad_text: str = Form("")):
    service.mark_applied(_store(request), app_id, ad_text)
    return reply(request, "", "/")


@router.post("/a/{app_id}/skip")
def skip(request: Request, app_id: str, reason: str = Form(...), other_text: str = Form("")):
    if reason == OTHER_REASON:
        reason = other_text.strip()
        if not reason:
            raise ValueError("Type a reason for 'other'.")
    elif reason not in SKIP_REASONS:
        raise ValueError("Unknown skip reason.")
    service.skip(_store(request), app_id, reason)
    return reply(request, "", "/")


@router.post("/a/{app_id}/open-cv")
def open_cv(request: Request, app_id: str):
    store = _store(request)
    rel = store.get_app(app_id)["cv_file"].strip()
    if not rel:
        return HTMLResponse("No CV file is set for this job.", status_code=404)
    base = store.data_dir.parent.resolve()
    path = (base / rel).resolve()
    if not path.is_relative_to(base):
        return HTMLResponse("That CV path is outside the Employment folder.", status_code=400)
    if not path.is_file():
        return HTMLResponse(f"CV file not found: {html.escape(rel)}", status_code=404)
    open_file(path)
    return HTMLResponse(f"Opened {html.escape(path.name)}")


@router.post("/skip-reasons/export")
def export_skip_reasons(request: Request):
    path = service.export_skip_reasons(_store(request))
    return reply(request, f"Saved {html.escape(path.name)} in the data folder.", "/")
```

- [ ] **Step 6: Templates and CSS**

`templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}Job Tracker{% endblock %}</title>
  <link rel="stylesheet" href="/static/app.css">
  <script src="https://unpkg.com/htmx.org@1.9.12" defer></script>
  <script>
    // Show server error messages (4xx) that HTMX would otherwise discard.
    document.addEventListener('htmx:beforeSwap', function (e) {
      var s = e.detail.xhr.status;
      if (s >= 400 && s < 500) { e.detail.shouldSwap = true; e.detail.isError = false; }
    });
  </script>
</head>
<body>
  <nav aria-label="Main">
    <strong>Job Tracker</strong>
    <a href="/" {% if active == 'queue' %}aria-current="page"{% endif %}>Queue</a>
    <a href="/followups" {% if active == 'followups' %}aria-current="page"{% endif %}>Follow-ups</a>
    <a href="/all" {% if active == 'all' %}aria-current="page"{% endif %}>All</a>
    <a href="/dashboard" {% if active == 'dashboard' %}aria-current="page"{% endif %}>Dashboard</a>
    <a href="/add" class="spacer">+ Add</a>
  </nav>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
```

`templates/error.html`:

```html
{% extends "base.html" %}
{% block title %}{{ title }}{% endblock %}
{% block content %}
<h1>{{ title }}</h1>
<p class="error" role="alert">{{ message }}</p>
{% if hint %}<p>{{ hint }}</p>{% endif %}
{% endblock %}
```

`templates/_card.html`:

```html
<article class="card" id="card-{{ r.id }}">
  <h3><a href="/a/{{ r.id }}">{{ r.company }}</a> <span class="role">{{ r.role }}</span></h3>
  <p class="meta">{{ r.location or 'Location n/a' }} · {{ r.source or 'Source n/a' }}{% if r.priority %} · {{ r.priority }}{% endif %}</p>
  {% if r.match_reason %}<p>{{ r.match_reason }}</p>{% endif %}
  <p>Suggested CV: <strong>{{ r.cv_variant or 'none' }}</strong>{% if r.cv_file %} <span class="meta">({{ r.cv_file }})</span>{% endif %}</p>
  <div class="actions">
    {% if r.job_link.lower().startswith(('http://', 'https://')) %}
      <a class="btn" href="{{ r.job_link }}" target="_blank" rel="noopener">Open ad</a>
    {% endif %}
    <button class="btn" type="button" hx-post="/a/{{ r.id }}/open-cv" hx-target="#msg-{{ r.id }}" hx-swap="innerHTML">Open CV</button>
    <span id="msg-{{ r.id }}" role="status" class="meta"></span>
  </div>
  <details>
    <summary>Mark applied</summary>
    <form method="post" action="/a/{{ r.id }}/apply" hx-post="/a/{{ r.id }}/apply" hx-target="#card-{{ r.id }}" hx-swap="outerHTML">
      <label>Job ad text (optional, saved for tailoring)
        <textarea name="ad_text" rows="4"></textarea></label>
      <button class="btn primary">Mark applied</button>
    </form>
  </details>
  <details>
    <summary>Skip</summary>
    <form method="post" action="/a/{{ r.id }}/skip" hx-post="/a/{{ r.id }}/skip" hx-target="#card-{{ r.id }}" hx-swap="outerHTML" class="reasons">
      {% for reason in skip_reasons if reason != 'other (type it)' %}
        <button class="btn" name="reason" value="{{ reason }}">{{ reason }}</button>
      {% endfor %}
    </form>
    <form method="post" action="/a/{{ r.id }}/skip" hx-post="/a/{{ r.id }}/skip" hx-target="#card-{{ r.id }}" hx-swap="outerHTML" class="reasons">
      <input type="hidden" name="reason" value="other (type it)">
      <label class="inline">Other <input name="other_text" placeholder="type a reason"></label>
      <button class="btn">Skip</button>
    </form>
  </details>
</article>
```

`templates/queue.html`:

```html
{% extends "base.html" %}
{% block title %}Queue · Job Tracker{% endblock %}
{% block content %}
<h1>Queue <span class="meta">{{ total }} ready</span></h1>
{% for label, rows in groups %}
  <h2>{{ label }} priority</h2>
  {% for r in rows %}{% include "_card.html" %}{% endfor %}
{% else %}
  <p>No jobs waiting. The scan adds suggestions here; you can also <a href="/add">add one yourself</a>.</p>
{% endfor %}

<section aria-labelledby="skip-h">
  <h2 id="skip-h">Skip reasons, last 30 days</h2>
  {% if tally %}
    <table>
      <thead><tr><th>Reason</th><th>Count</th><th>Examples</th></tr></thead>
      <tbody>
      {% for t in tally %}
        <tr><td>{{ t.reason }}</td><td>{{ t.count }}</td>
            <td>{% for e in t.examples %}{{ e.company }} - {{ e.role }}{% if not loop.last %}; {% endif %}{% endfor %}</td></tr>
      {% endfor %}
      </tbody>
    </table>
  {% else %}<p class="meta">Nothing skipped yet.</p>{% endif %}
  <button class="btn" type="button" hx-post="/skip-reasons/export" hx-target="#export-msg" hx-swap="innerHTML">Export skip_reasons.json</button>
  <span id="export-msg" role="status" class="meta"></span>
</section>
{% endblock %}
```

`static/app.css`:

```css
:root {
  --bg: #fafaf8; --fg: #1d1d1b; --muted: #6b6b66; --card: #fff; --line: #dcdcd6;
  --accent: #1f5fbf; --accent-fg: #fff; --danger: #b3261e; --ok: #1b7a3d;
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #161615; --fg: #ececE8; --muted: #a0a09a; --card: #212120; --line: #3a3a37;
          --accent: #6ea3f5; --accent-fg: #0c1b33; --danger: #f2877f; --ok: #6fd18d; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.5 system-ui, sans-serif; }
nav { display: flex; flex-wrap: wrap; gap: 1rem; align-items: center; padding: .75rem 1rem; border-bottom: 1px solid var(--line); background: var(--card); }
nav a { color: var(--fg); text-decoration: none; padding: .25rem .5rem; border-radius: 6px; }
nav a[aria-current="page"] { background: var(--accent); color: var(--accent-fg); }
nav .spacer { margin-left: auto; }
main { max-width: 64rem; margin: 0 auto; padding: 1rem; }
a { color: var(--accent); }
h1, h2, h3 { line-height: 1.25; }
.meta { color: var(--muted); font-size: .9rem; font-weight: normal; }
.error, .overdue { color: var(--danger); }
.ok { color: var(--ok); }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 1rem; margin: 0 0 1rem; }
.card h3 { margin: 0 0 .25rem; } .role { font-weight: normal; }
.actions { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; margin: .5rem 0; }
.btn { display: inline-block; font: inherit; padding: .4rem .8rem; border: 1px solid var(--line); border-radius: 8px; background: var(--card); color: var(--fg); cursor: pointer; text-decoration: none; }
.btn:hover, .btn:focus-visible { border-color: var(--accent); }
.btn.primary { background: var(--accent); color: var(--accent-fg); border-color: var(--accent); }
.btn.danger { color: var(--danger); }
.reasons { display: flex; flex-wrap: wrap; gap: .5rem; margin: .5rem 0; }
label { display: block; margin: .5rem 0; }
label.inline { display: inline-flex; gap: .5rem; align-items: center; margin: 0; }
input, textarea, select { font: inherit; padding: .4rem; border: 1px solid var(--line); border-radius: 6px; background: var(--card); color: var(--fg); max-width: 100%; }
textarea { width: 100%; }
table { width: 100%; border-collapse: collapse; margin: .5rem 0 1rem; }
th, td { text-align: left; padding: .4rem .5rem; border-bottom: 1px solid var(--line); vertical-align: top; }
th a { color: inherit; }
.banner { border: 1px solid var(--danger); border-left-width: 6px; border-radius: 6px; padding: .5rem .75rem; margin: 1rem 0; background: var(--card); }
progress { width: 100%; height: 1.2rem; }
.grid { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit, minmax(14rem, 1fr)); }
.stat { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: .75rem 1rem; }
.stat b { display: block; font-size: 1.8rem; }
.timeline { list-style: none; padding: 0; } .timeline li { padding: .4rem 0; border-bottom: 1px solid var(--line); }
@media (max-width: 40rem) { main { padding: .75rem; } table { display: block; overflow-x: auto; } }
```

- [ ] **Step 7: Run to verify pass**

Run: `uv run pytest tests/test_routes_queue.py -v`
Expected: all PASS. Common snags: `TemplateResponse(request, name, ctx)` is the current Starlette signature; the `'` quoting in `_card.html` for `startswith((...))` is valid Jinja.

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: web skeleton and queue page with apply, skip, open CV" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Detail page (features 3, 5, 7 and the editable follow-up fields)

**Files:**
- Modify: `src/job_tracker/routes.py` (append)
- Create: `src/job_tracker/templates/detail.html`
- Test: `tests/test_routes_detail.py`

**Interfaces:**
- Consumes: `service.save_ad`, `change_status`, `update_details`; `models.find_duplicates`, `tailor_prefix`, `STAGE_BUTTONS`; `Store.read_apps/read_events/read_ad`.
- Produces: `GET /a/{id}`, `POST /a/{id}/ad`, `POST /a/{id}/details`, `POST /a/{id}/status`, all redirecting (303) to `/a/{id}`.

- [ ] **Step 1: Write failing tests** `tests/test_routes_detail.py`

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_routes_detail.py -v`
Expected: FAIL (404s: routes missing).

- [ ] **Step 3: Append routes to `routes.py`**

Add imports at top: `from .models import STAGE_BUTTONS, find_duplicates, tailor_prefix` (merge with the existing models import). Append:

```python
def _detail_url(app_id: str) -> str:
    return f"/a/{app_id}"


@router.get("/a/{app_id}")
def detail(request: Request, app_id: str):
    store = _store(request)
    row = store.get_app(app_id)
    _, rows = store.read_apps()
    _, events = store.read_events()
    timeline = sorted((e for e in events if e["application_id"] == app_id), key=lambda e: (e["date"], e["id"]), reverse=True)
    return render(
        request, "detail.html", row=row, timeline=timeline, ad=store.read_ad(app_id), prefix=tailor_prefix(row),
        dups=find_duplicates(rows, row["company"], row["role"], store.today(), exclude_id=app_id),
        stage_buttons=STAGE_BUTTONS,
    )


@router.post("/a/{app_id}/ad")
def save_ad(request: Request, app_id: str, ad_text: str = Form("")):
    service.save_ad(_store(request), app_id, ad_text)
    return RedirectResponse(_detail_url(app_id), status_code=303)


@router.post("/a/{app_id}/status")
def set_status(request: Request, app_id: str, status: str = Form(...), note: str = Form("")):
    if status not in STAGE_BUTTONS:
        raise ValueError("Pick one of the stage buttons.")
    service.change_status(_store(request), app_id, status, note)
    return RedirectResponse(_detail_url(app_id), status_code=303)


@router.post("/a/{app_id}/details")
def save_details(
    request: Request, app_id: str,
    next_action: str = Form(""), next_action_date: str = Form(""), contact: str = Form(""), salary: str = Form(""),
    notes: str = Form(""), cv_variant: str = Form(""), cv_file: str = Form(""), job_link: str = Form(""),
):
    fields = dict(next_action=next_action, next_action_date=next_action_date, contact=contact, salary=salary,
                  notes=notes, cv_variant=cv_variant, cv_file=cv_file, job_link=job_link)
    service.update_details(_store(request), app_id, fields)
    return RedirectResponse(_detail_url(app_id), status_code=303)
```

Note: form fields missing from the post are not in `fields` only if you omit them; here all eight are always passed (empty means "clear"). The test posting only `next_action_date` therefore clears the others, which is the intended behaviour for a full-form post.

- [ ] **Step 4: Create `templates/detail.html`**

```html
{% extends "base.html" %}
{% block title %}{{ row.company }} · Job Tracker{% endblock %}
{% block content %}
<h1>{{ row.company }} <span class="meta">{{ row.role }}</span></h1>
<p><strong>{{ row.status }}</strong>
  {% if row.date_applied %} · applied {{ row.date_applied }}{% endif %}
  · {{ row.location or 'location n/a' }} · {{ row.source or 'source n/a' }}
  {% if row.job_link.lower().startswith(('http://', 'https://')) %}
    · <a href="{{ row.job_link }}" target="_blank" rel="noopener">Open ad</a>
  {% endif %}
  <button class="btn" type="button" hx-post="/a/{{ row.id }}/open-cv" hx-target="#cv-msg" hx-swap="innerHTML">Open CV</button>
  <span id="cv-msg" role="status" class="meta"></span>
</p>
{% if row.skip_reason %}<p class="meta">Skip reason: {{ row.skip_reason }}</p>{% endif %}

{% if dups %}
<div class="banner" role="alert">
  <strong>Possible duplicate:</strong>
  {% for d in dups %}<a href="/a/{{ d.id }}">{{ d.company }} - {{ d.role }}</a> ({{ d.status }}){% if not loop.last %}, {% endif %}{% endfor %}
</div>
{% endif %}

<section aria-labelledby="stage-h">
  <h2 id="stage-h">Move to a stage</h2>
  <form method="post" action="/a/{{ row.id }}/status" class="reasons">
    <label class="inline">Note <input name="note" placeholder="optional"></label>
    {% for s in stage_buttons %}<button class="btn" name="status" value="{{ s }}">{{ s }}</button>{% endfor %}
  </form>
</section>

<section aria-labelledby="fu-h">
  <h2 id="fu-h">Follow-up and details</h2>
  <form method="post" action="/a/{{ row.id }}/details">
    <label>Next action <input name="next_action" value="{{ row.next_action }}"></label>
    <label>Next action date <input type="date" name="next_action_date" value="{{ row.next_action_date }}"></label>
    <label>Contact <input name="contact" value="{{ row.contact }}"></label>
    <label>Salary <input name="salary" value="{{ row.salary }}"></label>
    <label>CV variant <input name="cv_variant" value="{{ row.cv_variant }}"></label>
    <label>CV file <input name="cv_file" value="{{ row.cv_file }}"></label>
    <label>Job link <input name="job_link" value="{{ row.job_link }}"></label>
    <label>Notes <textarea name="notes" rows="3">{{ row.notes }}</textarea></label>
    <button class="btn primary">Save details</button>
  </form>
</section>

<section aria-labelledby="ad-h">
  <h2 id="ad-h">Job ad</h2>
  <form method="post" action="/a/{{ row.id }}/ad">
    <label>Ad text <textarea id="ad-text" name="ad_text" rows="10">{{ ad }}</textarea></label>
    <button class="btn primary">Save ad</button>
    <button class="btn" type="button" id="copy-prompt" data-prefix="{{ prefix }}">Copy tailor prompt</button>
    <span id="copy-msg" role="status" class="meta"></span>
  </form>
</section>

<section aria-labelledby="tl-h">
  <h2 id="tl-h">Timeline</h2>
  <ul class="timeline">
    {% for e in timeline %}
      <li><strong>{{ e.date }}</strong> {{ e.from_status or 'new' }} → {{ e.to_status }}{% if e.note %} <span class="meta">{{ e.note }}</span>{% endif %}</li>
    {% else %}<li class="meta">No events yet.</li>{% endfor %}
  </ul>
</section>

<script>
  document.getElementById('copy-prompt').addEventListener('click', async function () {
    var text = this.dataset.prefix + '\n' + document.getElementById('ad-text').value;
    var msg = document.getElementById('copy-msg');
    try { await navigator.clipboard.writeText(text); msg.textContent = 'Copied.'; }
    catch (err) { msg.textContent = 'Copy failed: ' + err.message; }
  });
</script>
{% endblock %}
```

(`navigator.clipboard` works on `http://localhost`, which browsers treat as a secure context.)

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_routes_detail.py tests/test_routes_queue.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: detail page with ad text, timeline, status buttons, duplicate banner" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Follow-ups page (feature 4)

**Files:**
- Modify: `src/job_tracker/routes.py` (append)
- Create: `src/job_tracker/templates/followups.html`
- Test: `tests/test_routes_followups.py`

**Interfaces:**
- Consumes: `stats.followups`, `service.change_status`, `service.apply_rejection_rule`, `Store.settings`.
- Produces: `GET /followups`, `POST /a/{id}/reject` (HTMX removes the table row), `POST /followups/apply-rule` (303 to `/followups?rejected=<n>`).

- [ ] **Step 1: Write failing tests**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_routes_followups.py -v`
Expected: FAIL (404).

- [ ] **Step 3: Append routes**

```python
@router.get("/followups")
def followups_page(request: Request, rejected: str = ""):
    store = _store(request)
    _, rows = store.read_apps()
    _, events = store.read_events()
    settings = store.settings()
    return render(request, "followups.html", active="followups", days=settings["auto_reject_days"],
                  rejected=rejected, **stats.followups(rows, events, settings, store.today()))


@router.post("/a/{app_id}/reject")
def reject(request: Request, app_id: str):
    service.change_status(_store(request), app_id, "Rejected", "no reply")
    return reply(request, "", "/followups")


@router.post("/followups/apply-rule")
def apply_rule(request: Request):
    ids = service.apply_rejection_rule(_store(request))
    return RedirectResponse(f"/followups?rejected={len(ids)}", status_code=303)
```

- [ ] **Step 4: Create `templates/followups.html`**

```html
{% extends "base.html" %}
{% block title %}Follow-ups · Job Tracker{% endblock %}
{% block content %}
<h1>Follow-ups</h1>
{% if rejected != '' %}<p class="ok" role="status">Rejected {{ rejected }} job{{ '' if rejected == '1' else 's' }} under the {{ days }}-day rule.</p>{% endif %}

{% macro action_rows(items, css) %}
  {% for r in items %}
  <tr>
    <td><a href="/a/{{ r.id }}">{{ r.company }}</a> <span class="meta">{{ r.role }}</span></td>
    <td>{{ r.next_action or '-' }}</td>
    <td class="{{ css }}">{{ r.next_action_date }}</td>
  </tr>
  {% endfor %}
{% endmacro %}

<h2 class="overdue">Overdue ({{ overdue|length }})</h2>
{% if overdue %}<table><thead><tr><th>Job</th><th>Action</th><th>Due</th></tr></thead><tbody>{{ action_rows(overdue, 'overdue') }}</tbody></table>
{% else %}<p class="meta">Nothing overdue.</p>{% endif %}

<h2>Due this week ({{ due_week|length }})</h2>
{% if due_week %}<table><thead><tr><th>Job</th><th>Action</th><th>Due</th></tr></thead><tbody>{{ action_rows(due_week, '') }}</tbody></table>
{% else %}<p class="meta">Nothing due in the next 7 days.</p>{% endif %}

<h2>About to auto-reject ({{ about_to_reject|length }})</h2>
<p class="meta">Applied jobs {{ days - 5 }} to {{ days - 1 }} days old with no reply.</p>
{% if about_to_reject %}
<table>
  <thead><tr><th>Job</th><th>Applied</th><th></th></tr></thead>
  <tbody>
  {% for r in about_to_reject %}
    <tr>
      <td><a href="/a/{{ r.id }}">{{ r.company }}</a> <span class="meta">{{ r.role }}</span></td>
      <td>{{ r.date_applied }}</td>
      <td><button class="btn danger" hx-post="/a/{{ r.id }}/reject" hx-target="closest tr" hx-swap="outerHTML">Mark rejected</button></td>
    </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}

<form method="post" action="/followups/apply-rule">
  <button class="btn danger">Apply {{ days }}-day rule ({{ rule_candidates|length }} job{{ '' if rule_candidates|length == 1 else 's' }} would be rejected)</button>
</form>
{% endblock %}
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_routes_followups.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: follow-ups page with 30-day rule" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 8: All applications and Add form (features 7 and 8)

**Files:**
- Modify: `src/job_tracker/routes.py` (append)
- Create: `src/job_tracker/templates/{all,add}.html`
- Test: `tests/test_routes_all.py`

**Interfaces:**
- Consumes: `stats.filter_rows`, `stats.SORTABLE`, `service.add_application`, `models.STATUSES`.
- Produces: `GET /all` (query: `q,status,source,cv_variant,date_from,date_to,sort,dir`), `GET /add`, `POST /add` (303 to `/a/<new id>`).

- [ ] **Step 1: Write failing tests**

```python
def test_all_lists_everything_with_count_and_unknown_status_row(client, seed):
    seed(company="One")
    seed(company="Two", status="Applied")
    html = client.get("/all").text
    assert "2 results" in html and "One" in html and "Two" in html


def test_all_and_add_render_on_first_run(client, store):
    assert not store.data_dir.exists()
    for path in ("/all", "/add", "/followups", "/dashboard"):
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
    seed(company="Odd", status="Pending", date_applied="unknown")
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_routes_all.py -v`
Expected: FAIL (404).

- [ ] **Step 3: Append routes**

Add imports `from urllib.parse import urlencode` and `from fastapi import Query`, `from .models import STATUSES`.

```python
@router.get("/all")
def all_page(
    request: Request, q: str = "", status: str = "", source: str = "", cv_variant: str = "",
    date_from: str = "", date_to: str = "", sort: str = "last_update", direction: str = Query("desc", alias="dir"),
):
    _, rows = _store(request).read_apps()
    found = stats.filter_rows(rows, q, status, source, cv_variant, date_from, date_to, sort, direction)
    current = {"q": q, "status": status, "source": source, "cv_variant": cv_variant, "date_from": date_from, "date_to": date_to}
    current = {k: v for k, v in current.items() if v}
    sort_urls = {
        col: "/all?" + urlencode({**current, "sort": col, "dir": "asc" if (sort == col and direction == "desc") else "desc"})
        for col in stats.SORTABLE
    }
    return render(
        request, "all.html", active="all", rows=found, f=current, sort=sort, direction=direction, sort_urls=sort_urls,
        statuses=STATUSES, sources=sorted({r["source"] for r in rows if r["source"]}),
        cvs=sorted({r["cv_variant"] for r in rows if r["cv_variant"]}),
    )


@router.get("/add")
def add_form(request: Request):
    return render(request, "add.html", statuses=["Ready", "Applied", "Incomplete"])


@router.post("/add")
def add(
    request: Request, company: str = Form(""), role: str = Form(""), status: str = Form("Ready"),
    location: str = Form(""), source: str = Form(""), job_link: str = Form(""), cv_variant: str = Form(""),
    cv_file: str = Form(""), notes: str = Form(""),
):
    app_id = service.add_application(_store(request), dict(
        company=company, role=role, status=status, location=location, source=source,
        job_link=job_link, cv_variant=cv_variant, cv_file=cv_file, notes=notes))
    return RedirectResponse(_detail_url(app_id), status_code=303)
```

(The duplicate banner appears on the detail page the add redirects to, which satisfies "when adding".)

- [ ] **Step 4: Templates**

`templates/all.html`:

```html
{% extends "base.html" %}
{% block title %}All applications · Job Tracker{% endblock %}
{% block content %}
<h1>All applications</h1>
<form method="get" action="/all" class="reasons" role="search">
  <label class="inline">Search <input type="search" name="q" value="{{ f.q or '' }}" placeholder="company, role, notes"></label>
  <label class="inline">Status
    <select name="status"><option value="">Any</option>
      {% for s in statuses %}<option {% if f.status == s %}selected{% endif %}>{{ s }}</option>{% endfor %}</select></label>
  <label class="inline">Source
    <select name="source"><option value="">Any</option>
      {% for s in sources %}<option {% if f.source == s %}selected{% endif %}>{{ s }}</option>{% endfor %}</select></label>
  <label class="inline">CV
    <select name="cv_variant"><option value="">Any</option>
      {% for s in cvs %}<option {% if f.cv_variant == s %}selected{% endif %}>{{ s }}</option>{% endfor %}</select></label>
  <label class="inline">From <input type="date" name="date_from" value="{{ f.date_from or '' }}"></label>
  <label class="inline">To <input type="date" name="date_to" value="{{ f.date_to or '' }}"></label>
  <input type="hidden" name="sort" value="{{ sort }}"><input type="hidden" name="dir" value="{{ direction }}">
  <button class="btn primary">Filter</button>
  <a class="btn" href="/all">Reset</a>
</form>
<p role="status">{{ rows|length }} result{{ '' if rows|length == 1 else 's' }}</p>
<table>
  <thead><tr>
    {% for col, label in [('company','Company'),('role','Role'),('status','Status'),('source','Source'),('cv_variant','CV'),('date_applied','Applied'),('last_update','Updated')] %}
      <th scope="col" {% if sort == col %}aria-sort="{{ 'ascending' if direction == 'asc' else 'descending' }}"{% endif %}>
        <a href="{{ sort_urls[col] }}">{{ label }}</a></th>
    {% endfor %}
  </tr></thead>
  <tbody>
  {% for r in rows %}
    <tr><td><a href="/a/{{ r.id }}">{{ r.company }}</a></td><td>{{ r.role }}</td><td>{{ r.status }}</td><td>{{ r.source }}</td>
        <td>{{ r.cv_variant }}</td><td>{{ r.date_applied }}</td><td>{{ r.last_update }}</td></tr>
  {% endfor %}
  </tbody>
</table>
{% endblock %}
```

`templates/add.html`:

```html
{% extends "base.html" %}
{% block title %}Add application · Job Tracker{% endblock %}
{% block content %}
<h1>Add application</h1>
<form method="post" action="/add">
  <label>Company <input name="company" required></label>
  <label>Role <input name="role" required></label>
  <label>Status
    <select name="status">{% for s in statuses %}<option>{{ s }}</option>{% endfor %}</select></label>
  <label>Location <input name="location"></label>
  <label>Source <input name="source" placeholder="Seek, LinkedIn, Company site"></label>
  <label>Job link <input type="url" name="job_link"></label>
  <label>CV variant <input name="cv_variant"></label>
  <label>CV file <input name="cv_file" placeholder="Tailored CVs/..."></label>
  <label>Notes <textarea name="notes" rows="3"></textarea></label>
  <button class="btn primary">Add</button>
</form>
{% endblock %}
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/test_routes_all.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: all-applications search and filters, add form" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Dashboard page (feature 6)

**Files:**
- Modify: `src/job_tracker/routes.py` (append)
- Create: `src/job_tracker/templates/dashboard.html`
- Test: `tests/test_routes_dashboard.py`

**Interfaces:**
- Consumes: `stats.dashboard`, `Store.settings`.
- Produces: `GET /dashboard`.

- [ ] **Step 1: Write failing tests**

```python
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
```

(In `test_dashboard_numbers...` 4 submitted: 3 Applied + 1 Interview; positive 1; rate 25%; rule: 4 ≥ 3 and 1·3 ≥ 1·4 is false → `cv_problem`. That is fine, the test does not assert the rule text. Don't "fix" it.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_routes_dashboard.py -v`
Expected: FAIL (404).

- [ ] **Step 3: Append route**

```python
@router.get("/dashboard")
def dashboard(request: Request):
    store = _store(request)
    _, rows = store.read_apps()
    return render(request, "dashboard.html", active="dashboard", d=stats.dashboard(rows, store.settings(), store.today()))
```

- [ ] **Step 4: Create `templates/dashboard.html`**

```html
{% extends "base.html" %}
{% block title %}Dashboard · Job Tracker{% endblock %}
{% block content %}
<h1>Dashboard</h1>

<section aria-labelledby="week-h">
  <h2 id="week-h">This week</h2>
  <p>{{ d.week_applied }} of {{ d.week_target }} applications</p>
  <progress value="{{ [d.week_applied, d.week_target]|min }}" max="{{ [d.week_target, 1]|max }}" aria-label="Applications this week against target"></progress>
</section>

<div class="grid">
  <div class="stat"><b>{{ d.submitted }}</b>submitted</div>
  <div class="stat"><b>{{ d.positive }}</b>positive</div>
  <div class="stat"><b>{{ d.rate|pct }}</b>positive rate</div>
  <div class="stat"><b>{{ d.ready }}</b><a href="/">in the Ready queue</a></div>
  <div class="stat"><b class="{{ 'overdue' if d.overdue else '' }}">{{ d.overdue }}</b><a href="/followups">overdue follow-ups</a></div>
</div>

<p role="status">
  {% if d.rule == 'too_early' %}Rule check: too early (needs {{ d.rule_per }} submitted; you have {{ d.submitted }}).
  {% elif d.rule == 'cv_problem' %}<strong class="error">Rule check: CV problem, not the market.</strong>
    Fewer than {{ d.rule_min }} positive per {{ d.rule_per }} submitted.
  {% else %}<span class="ok">Rule check: OK</span>, at least {{ d.rule_min }} positive per {{ d.rule_per }} submitted.{% endif %}
</p>

<section aria-labelledby="chart-h">
  <h2 id="chart-h">Applications per week (last 12 weeks)</h2>
  <canvas id="weekly" role="img" aria-label="Bar chart of applications per week"></canvas>
</section>

<section aria-labelledby="funnel-h">
  <h2 id="funnel-h">Funnel</h2>
  <table>
    <thead><tr><th>Stage</th><th>Count</th></tr></thead>
    <tbody>
      <tr><td>Submitted</td><td>{{ d.funnel.submitted }}</td></tr>
      <tr><td>Positive (assessment or better)</td><td>{{ d.funnel.positive }}</td></tr>
      <tr><td>Interview or better</td><td>{{ d.funnel.interview }}</td></tr>
      <tr><td>Offer</td><td>{{ d.funnel.offer }}</td></tr>
    </tbody>
  </table>
</section>

{% macro breakdown(title, rows) %}
<section>
  <h2>{{ title }}</h2>
  <table>
    <thead><tr><th>Group</th><th>Submitted</th><th>Positive</th><th>Rate</th></tr></thead>
    <tbody>
    {% for x in rows %}<tr><td>{{ x.key }}</td><td>{{ x.submitted }}</td><td>{{ x.positive }}</td><td>{{ x.rate|pct }}</td></tr>
    {% else %}<tr><td colspan="4" class="meta">No submissions yet.</td></tr>{% endfor %}
    </tbody>
  </table>
</section>
{% endmacro %}
{{ breakdown('By CV variant', d.by_cv) }}
{{ breakdown('By source', d.by_source) }}
{{ breakdown('By role type', d.by_role) }}

<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script>
  if (window.Chart) {
    new Chart(document.getElementById('weekly'), {
      type: 'bar',
      data: {
        labels: {{ d.weekly|map(attribute='week')|list|tojson }},
        datasets: [{ label: 'Applications', data: {{ d.weekly|map(attribute='count')|list|tojson }},
                     backgroundColor: getComputedStyle(document.documentElement).getPropertyValue('--accent') }]
      },
      options: { scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }, plugins: { legend: { display: false } } }
    });
  }
</script>
{% endblock %}
```

- [ ] **Step 5: Run to verify pass, then run the whole suite so far**

Run: `uv run pytest tests/test_routes_dashboard.py -v && uv run pytest -q`
Expected: dashboard tests PASS; whole suite green.

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: dashboard page" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 10: `cli.py` and lifecycle (launch, stop, status, launcher)

**Files:**
- Create: `src/job_tracker/cli.py`
- Test: `tests/test_cli.py`
- Modify: `docs/superpowers/specs/2026-10-11-job-tracker-design.md` (section 7: replace the pidfile wording)

**Interfaces:**
- Consumes: `server` module (`python -m job_tracker.server --port N`).
- Produces: `cli.main(argv=None) -> int` (entry point `jobs`); `cli.is_running() -> bool`; `cli.start_server() -> None`; `cli.stop_server() -> bool`; `cli.open_browser(url) -> None` (patched in tests); `cli.launcher_source(jobs_path) -> str`; `cli.install_launcher() -> Path`. Env: `JOBS_PORT` (default 8765), `JOBS_LOG_FILE` (default `~/Library/Logs/job-tracker.log`), `JOBS_DATA_DIR`.
- Design change vs spec: no pidfile. The listener PID comes from `lsof -ti tcp:PORT -sTCP:LISTEN`, and only after `/health` confirms it is our app, so a recycled PID can never be signalled.

- [ ] **Step 1: Write failing tests** `tests/test_cli.py`

```python
import os
import socket
import subprocess
import sys
import threading
import time

import httpx
import pytest
from filelock import FileLock

from job_tracker import cli


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def env(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("JOBS_PORT", str(port))
    monkeypatch.setenv("JOBS_DATA_DIR", str(tmp_path / "Employment" / "Job Tracker"))
    monkeypatch.setenv("JOBS_LOG_FILE", str(tmp_path / "server.log"))
    opened = []
    monkeypatch.setattr(cli, "open_browser", opened.append)
    yield {"port": port, "opened": opened, "data": tmp_path / "Employment" / "Job Tracker"}
    cli.stop_server()  # never leave a server behind


def test_status_when_not_running(env, capsys):
    assert cli.main(["status"]) == 0
    assert "not running" in capsys.readouterr().out


def test_is_running_ignores_foreign_listener(env):
    import http.server

    srv = http.server.HTTPServer(("127.0.0.1", env["port"]), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert cli.is_running() is False   # something answers on the port, but it is not Job Tracker
    finally:
        srv.shutdown()


def test_jobs_starts_server_once_then_only_opens_browser(env, monkeypatch):
    assert cli.main([]) == 0
    assert cli.is_running()
    assert env["opened"] == [f"http://localhost:{env['port']}"]

    spawned = []
    with monkeypatch.context() as m:  # scoped, so the fixture's cleanup can still spawn `lsof`
        m.setattr(cli.subprocess, "Popen", lambda *a, **k: spawned.append(a) or pytest.fail("second server started"))
        assert cli.main([]) == 0
    assert spawned == [] and len(env["opened"]) == 2   # browser opened again, no new server


def test_jobs_stop_frees_the_port(env, capsys):
    cli.main([])
    assert cli.main(["stop"]) == 0
    assert not cli.is_running()
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", env["port"])) != 0
    assert cli.main(["stop"]) == 0 and "not running" in capsys.readouterr().out


def test_stop_during_a_write_lets_the_write_finish(env):
    cli.main([])
    base = f"http://127.0.0.1:{env['port']}"
    # create a job to update, then hold the data lock so the next request is stuck mid-write
    app_id = httpx.post(base + "/add", data={"company": "Acme", "role": "Dev", "status": "Ready"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    result = {}

    with FileLock(str(env["data"] / ".tracker.lock")):
        def slow_write():
            result["r"] = httpx.post(f"{base}/a/{app_id}/status", data={"status": "Interview"}, timeout=30, follow_redirects=False)

        t = threading.Thread(target=slow_write)
        t.start()
        time.sleep(1)                                   # the request is now waiting on the lock
        stopper = threading.Thread(target=cli.stop_server)
        stopper.start()                                 # SIGTERM while the write is in flight
        time.sleep(1)
        assert stopper.is_alive()                       # server is draining, not dead
    t.join(timeout=30)
    stopper.join(timeout=30)
    assert result["r"].status_code == 303
    rows = [l for l in (env["data"] / "applications.csv").read_text().splitlines() if app_id in l]
    assert "Interview" in rows[0]
    assert not cli.is_running()


def test_launcher_source_quotes_path():
    src = cli.launcher_source('/Users/a b/.local/bin/jo"bs')
    assert src == 'do shell script quoted form of "/Users/a b/.local/bin/jo\\"bs"\n'
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL (`ModuleNotFoundError: job_tracker.cli`).

- [ ] **Step 3: Implement `src/job_tracker/cli.py`**

```python
"""The `jobs` command. Starts the server on demand and stops it on request; nothing runs at boot."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path



def port() -> int:
    return int(os.environ.get("JOBS_PORT", "8765"))


def url() -> str:
    return f"http://localhost:{port()}"


def log_file() -> Path:
    return Path(os.environ.get("JOBS_LOG_FILE") or Path.home() / "Library/Logs/job-tracker.log")


def open_browser(target: str) -> None:
    webbrowser.open(target)


def is_running() -> bool:
    """True only if our own app answers /health (a foreign program on the port does not count)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port()}/health", timeout=1) as resp:
            return json.load(resp).get("app") == "job-tracker"
    except (OSError, ValueError, http.client.HTTPException):
        return False


def start_server() -> None:
    log = log_file()
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("ab") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "job_tracker.server", "--port", str(port())],
            stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
        )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if is_running():
            return
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    raise SystemExit(f"The server did not start. See {log}")


def _listener_pids() -> list[int]:
    out = subprocess.run(["lsof", "-ti", f"tcp:{port()}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
    return sorted({int(p) for p in out.split()})


def _alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")  # a zombie is already dead


def stop_server() -> bool:
    """SIGTERM the server and wait until it has exited. uvicorn finishes in-flight requests first."""
    if not is_running():
        return False
    pids = _listener_pids()
    if not pids:
        raise SystemExit("Job Tracker is running but its process could not be found; stop it from Activity Monitor.")
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.1)
    raise SystemExit("Job Tracker did not stop within 30 seconds.")


def launcher_source(jobs_path: str) -> str:
    escaped = jobs_path.replace("\\", "\\\\").replace('"', '\\"')
    return f'do shell script quoted form of "{escaped}"\n'


def install_launcher() -> Path:
    jobs_path = shutil.which("jobs") or str(Path(sys.argv[0]).resolve())
    dest = Path.home() / "Applications" / "Job Tracker.app"
    dest.parent.mkdir(exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)  # only ever our own launcher, at this exact path
    with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
        f.write(launcher_source(jobs_path))
    try:
        subprocess.run(["osacompile", "-o", str(dest), f.name], check=True)
    finally:
        os.unlink(f.name)
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jobs", description="Local job application tracker")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("stop", help="stop the server")
    sub.add_parser("status", help="say whether the server is running")
    sub.add_parser("install-launcher", help="create ~/Applications/Job Tracker.app for Spotlight")
    args = parser.parse_args(argv)

    if args.cmd == "stop":
        print("Stopped." if stop_server() else "Job Tracker is not running.")
    elif args.cmd == "status":
        print(f"Job Tracker is running at {url()}" if is_running() else "Job Tracker is not running.")
    elif args.cmd == "install-launcher":
        print(f"Created {install_launcher()}. Press Cmd+Space and type 'Job Tracker'.")
    else:
        if not is_running():
            start_server()
        open_browser(url())
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: all PASS. If `test_stop_during_a_write...` fails because the request is cut off, investigate how uvicorn drains (it should wait for the open connection); do not weaken the test. If the sandbox blocks `lsof`, run the suite outside the sandbox.

- [ ] **Step 5: Update the spec**

In `docs/superpowers/specs/2026-10-11-job-tracker-design.md` section 7, replace the `jobs stop` bullet and the "Pidfile location" bullet with: "`jobs stop`: confirms `/health` identifies Job Tracker, finds the listening process with `lsof -ti tcp:PORT -sTCP:LISTEN`, sends SIGTERM and waits for it to exit. uvicorn finishes the in-flight request first. No pidfile is used, so a recycled PID can never be signalled."

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: jobs CLI (start, stop, status, import, launcher)" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 11: Install, README, full-suite run and real-data walkthrough

**Files:**
- Create: `README.md`
- No source changes expected; fixes found during the walkthrough get their own test first, then a fix, then commit.

- [ ] **Step 1: Run the full suite and show the output to the user**

Run: `uv run pytest -v`
Expected: every test passes. Paste the final summary (counts) into the reply. Do not claim done before this.

- [ ] **Step 2: Install the command**

Run: `uv tool install --editable .` then `jobs status`
Expected: `Job Tracker is not running.` If `jobs` is not found, run `uv tool update-shell` and tell the user to open a new terminal (do not edit their shell files without asking).

- [ ] **Step 3: Build a safe copy of the data folder**

```bash
SCRATCH=/private/tmp/claude-501/-Users-binhphan-projects-application-tracker/fad887d8-5987-49ae-a435-e595c1ef1c3e/scratchpad
SRC="$HOME/Library/CloudStorage/GoogleDrive-binhp8699@gmail.com/My Drive/DriveSyncFiles/Obsidian/Plans/Employment"
mkdir -p "$SCRATCH/Employment/Job Tracker"
cp -R "$SRC/Tailored CVs" "$SCRATCH/Employment/" 2>/dev/null || mkdir -p "$SCRATCH/Employment/Tailored CVs"
export JOBS_DATA_DIR="$SCRATCH/Employment/Job Tracker" JOBS_PORT=8765
```

If the user's agent has already created CSVs in `$SRC/Job Tracker/`, copy `applications.csv`, `events.csv`, `job_ads/` and `settings.json` from there (read-only copy; never write to the original) and confirm the app reads them. Otherwise generate a throwaway sample dataset (not committed) with a script in the scratchpad that uses `Store` and `service` to create about 40 jobs covering every status, some with old `date_applied` values (including 25, 29, 30 and 31 days), CV files that exist and do not exist, a duplicate pair, and one row with an extra unknown column added by hand to the CSV. Confirm `$SRC/Job Tracker` was not modified.

- [ ] **Step 4: Start the app against the copy and walk through every feature**

Run `jobs` (with the exports above still set), then drive it with the built-in browser or Chrome skill (or `curl` if no browser tool is available). For each item, record what you clicked and what you saw:

1. Queue (`/`): Ready jobs, newest first, grouped by priority; Open ad link; Open CV on a job whose CV exists and on one whose CV is missing (expect an error message); Mark applied on one job with ad text; confirm the card disappears, and `applications.csv` row and `events.csv` row exist.
2. Skip: click a fixed reason on one job, type an "other" reason on another; check the Skip reasons panel after reload; click Export and open `skip_reasons.json`.
3. Detail page: paste and save an ad, reload, check it persisted in `job_ads/<id>.md`; click Copy tailor prompt and paste it into a text field to verify the exact format.
4. Follow-ups: set a next action and an overdue date on a job, check it appears in red; check the "about to auto-reject" list; use Mark rejected on one; click Apply 30-day rule and check the count and events.
5. Timeline: press Interview with a note, then Offer; confirm newest first and that nothing is deletable.
6. Dashboard: compare totals with a manual count of the CSV (e.g. `awk`/`python -c` over `applications.csv`); check the progress bar, the rule message, the 12-week chart rendering, funnel, three breakdown tables.
7. Duplicate banner: add a job with the same company and a similar role as a recent one; banner shows with a working link and the job is still added.
8. All: search text, each filter, sort by two columns, result count; Add form creates a job.
9. Resilience: add a row to the CSV by hand in the editor while the app is open, then act on another row in the UI; both survive. Corrupt a copy of the CSV (never the real one), reload, see the error page, confirm the file is untouched, restore it.
10. Lifecycle: run `jobs` again (browser opens, no second server: `lsof -ti tcp:8765 -sTCP:LISTEN` shows one pid); `jobs install-launcher` and confirm `~/Applications/Job Tracker.app` exists (launching it via Spotlight is a manual check for the user); `jobs stop`, then `jobs status`.

Fix anything broken: write a failing test, fix, rerun the full suite, commit.

- [ ] **Step 5: Write `README.md`**

```markdown
# Job Tracker

A local web app for the job hunt. CSV files in a Google Drive vault are the database; the app is
FastAPI + Jinja2 + HTMX. Single user, no login, `http://localhost:8765`.

## Install

    uv sync
    uv tool install --editable .

## Start and stop

    jobs            # starts the server if needed, opens the browser
    jobs status
    jobs stop       # stops the server (finishes any request in flight first)

Spotlight: run `jobs install-launcher` once, then press Cmd+Space, type "Job Tracker", Enter.

The server does **not** stop by itself. It only runs after you start it, and nothing starts at boot.
Logs: `~/Library/Logs/job-tracker.log`.

## Data

Folder: `$JOBS_DATA_DIR`, default
`~/Library/CloudStorage/GoogleDrive-binhp8699@gmail.com/My Drive/DriveSyncFiles/Obsidian/Plans/Employment/Job Tracker`.
CVs: `../Tailored CVs/` (the `cv_file` column is relative to `Plans/Employment/`).

| File | Purpose |
|---|---|
| `applications.csv` | one row per job |
| `events.csv` | append-only status timeline |
| `job_ads/<id>.md` | pasted job ad text |
| `settings.json` | `{"weekly_target": 8, "auto_reject_days": 30, "positive_rule": {"per": 40, "min_positive": 4}}` |
| `skip_reasons.json` | exported skip tally, for the scan |
| `backups/YYYY-MM-DD/` | copy of the CSVs before the first write each day (14 days kept) |

### applications.csv columns

`id, status, date_applied, company, role, level, location, work_mode, source, job_link, job_id, cv_variant, cv_file, priority, match_reason, next_action, next_action_date, contact, salary, skip_reason, notes, created_at, last_update`

Statuses: `Ready` (suggested, not applied), `Applied`, `Assessment`, `Interview`, `Final`, `Offer`,
`Rejected`, `No response`, `Withdrawn`, `Skipped`, `Incomplete`. Positive = Assessment, Interview,
Final, Offer. Submitted = everything except Ready, Skipped, Incomplete. Dates are `YYYY-MM-DD`.

### events.csv columns

`id, application_id, date, from_status, to_status, note`. Never delete or edit rows.

## Single source of truth

The CSVs are the only data. There is no Excel import; the agent creates and edits `applications.csv` and
`events.csv` directly, following the protocol below. For the dashboard and the 30-day rule to work, every
submitted job needs an ISO `date_applied`; rows without one are listed everywhere but excluded from weekly
counts and from the rule. When migrating the old Excel rows whose apply date is unknown, set `date_applied` to
`2026-10-08` (the user's chosen date; they were all applied around then). Use exactly the status names above (an unknown status is shown in All but ignored by stats).

## How the scan must write

The app and the scan share the same files, so both follow this protocol for **every** write:

1. Take the lock: `filelock` on `<data dir>/.tracker.lock`.
2. **Re-read** the CSV from disk (never reuse rows read earlier).
3. Change or append **by `id`**, never by row number. New ids are `a` + (highest existing number + 1), zero-padded to 4 digits (`a0042`); event ids likewise with `e`. Compute them while holding the lock.
4. Write a temp file in the same folder, `fsync`, then `os.replace` it onto the real file.
5. Release the lock.

Keep unknown columns as they are; add new columns only at the end of the header. Every status change also appends an event (`from_status`, `to_status`, `note`). A new suggestion is a row with
`status=Ready`, `created_at` and `last_update` set to today, plus an event (`from_status` empty, `to_status` `Ready`).
The easiest way is to use `job_tracker.storage.Store(...).transaction()`, which does all of the above.
A writer that skips the lock can lose changes.

Known gap: a status change updates `applications.csv` then `events.csv` inside one lock; a crash between the two
file replaces could leave a status change without its event.

## Develop

    uv run pytest
```

- [ ] **Step 6: Final check and commit**

Run: `uv run pytest -q` once more, and `git status` (confirm no stray files; the scratch copy lives outside the repo).

```bash
git add -A
git commit -m "docs: README and walkthrough fixes" -m "Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

Report to the user: full pytest output summary, the walkthrough list (what was clicked, what was seen), anything fixed, and the manual Spotlight check they still need to do.
