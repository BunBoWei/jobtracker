# Job Tracker: Design Spec

Date: 2026-10-11

## 1. Purpose

A local, single-user web app at `http://localhost:8765` that replaces the Excel job log; the CSVs are the single source of truth and the Excel file is not used or imported. A scheduled AI scan writes `Ready` suggestions into CSV files; the user reviews them, applies by hand, and tracks follow-ups and funnel stats to tell whether a poor result is a CV problem or a market problem. CSV files in a Google Drive synced Obsidian vault are the database.

Success criteria:
- Full test suite passes (output shown before claiming done).
- A concurrent write by the scan never loses data (tested).
- The app reads and writes the user's CSVs (created and edited directly by the agent) without losing data.
- All 8 features work end to end (walked through manually, findings reported).

## 2. Constraints and decisions

- Python 3.12, FastAPI, Jinja2, HTMX, Chart.js (CDN). No React, no build step, no DB server, no ORM. Managed with `uv`; tests with `pytest`.
- Dependencies: fastapi, jinja2, filelock, uvicorn, python-multipart. Dev: pytest, httpx. (uvicorn, python-multipart, httpx approved by the user on 2026-10-11. Any further dependency needs approval first.)
- App code lives in this repo. Only data lives in the vault. There is no Excel import; the agent creates and edits the CSVs directly.
- Data dir from env `JOBS_DATA_DIR`, default `~/Library/CloudStorage/GoogleDrive-binhp8699@gmail.com/My Drive/DriveSyncFiles/Obsidian/Plans/Employment/Job Tracker`. CVs resolve against `<data dir>/../` (paths in `cv_file` are relative to `Plans/Employment/`, so the base is the parent of the data dir).
- **Start/stop (changed during brainstorming):** the server is started on demand and runs until `jobtracker stop`. There is no heartbeat, no idle detection and no auto-shutdown. Nothing starts at boot (no LaunchAgent, no login item).

## 3. Modules

| Module | Responsibility |
|---|---|
| `storage.py` | Only module touching data files. `read_csv`, `mutate`, daily backups, `job_ads/`, `settings.json`. |
| `models.py` | Column lists, status constants (positive and submitted sets), `normalise_company`, `find_duplicates`. |
| `service.py` | Actions: add, mark applied, skip, change status, edit details, 30-day rule, save ad text. Each is one locked mutation that also appends the event. |
| `stats.py` | Dashboard numbers, role-type derivation, skip-reason tally and export. Pure functions over rows. |
| `routes.py` | FastAPI routes, Jinja templates, HTMX partials, corrupt-CSV error page. |
| `server.py` | App factory, `/health`, uvicorn entry point. |
| `cli.py` | `jobtracker`, `jobtracker stop`, `jobtracker status`, `jobtracker install-launcher`. |

## 4. Data model

UTF-8 CSV, header row, ISO dates.

`applications.csv` columns: `id, status, date_applied, company, role, level, location, work_mode, source, job_link, job_id, cv_variant, cv_file, priority, match_reason, next_action, next_action_date, contact, salary, skip_reason, notes, created_at, last_update`.

Statuses: `Ready, Applied, Assessment, Interview, Final, Offer, Rejected, No response, Withdrawn, Skipped, Incomplete`. Positive = Assessment, Interview, Final, Offer. Submitted = everything except Ready, Skipped, Incomplete.

`events.csv`: `id, application_id, date, from_status, to_status, note`. Append-only; events are never deleted or edited.

`job_ads/<application_id>.md`: pasted ad text.

`settings.json`: `{"weekly_target": 8, "auto_reject_days": 30, "positive_rule": {"per": 40, "min_positive": 4}}`. Missing file falls back to these defaults (not written until the user changes something).

IDs: `a0001`-style, generated inside the lock as max existing numeric suffix + 1, so a scan adding rows concurrently cannot collide. Event ids likewise (`e0001`).

## 5. Safe writes

Every write:
1. Take `filelock` on `<data dir>/.tracker.lock`.
2. Re-read the CSV from disk (never trust in-memory rows from an earlier request).
3. Apply the change by `id`, never by row position.
4. Write a temp file in the same folder, `fsync`, `os.replace` onto the real file.
5. Release the lock.

Further rules:
- A CSV that fails to parse raises `CorruptCsvError`. Pages show a clear error naming the file and the problem. The file is never overwritten.
- Before the first write of each day, copy each CSV to `backups/YYYY-MM-DD/`; prune to 14 days.
- Unknown extra columns round-trip untouched: rows are dicts over the file's own header, and new columns the app does not know stay in the output header.
- A status change updates `applications.csv` and appends to `events.csv` inside one lock hold. A crash between the two replaces could leave a status change without its event; this is accepted and documented in the README.
- The scan follows the same protocol (documented in the README).

## 6. Features

1. **Queue (home):** `Ready` jobs, newest first, grouped by priority. Card shows company, role, location, source, match reason, suggested CV. Buttons: Open ad (new tab), Open CV (server runs macOS `open`; error if the file is missing), Mark applied (sets Applied, `date_applied` and `last_update` to today, writes an event, optional ad-text field saved to `job_ads/<id>.md`).
2. **Skip with reason:** one-click reasons: needs citizenship/PR, security clearance, .NET/C# required, interstate on-site, too senior, too junior, not software, other (type it). Sets Skipped + `skip_reason` + event. Skip-reasons panel tallies the last 30 days; an Export button writes `skip_reasons.json` (count per reason plus example company/role) into the data dir.
3. **Job ad text:** detail-page textarea saved to `job_ads/<id>.md`. "Copy tailor prompt" copies `Tailor my CV for <company> - <role> (tracker id <id>). Job ad:` followed by the ad text.
4. **Follow-ups:** `next_action` and `next_action_date` editable on the detail page. Follow-ups page lists overdue (red) and due this week. A second list shows Applied jobs aged 25-30 days with a Mark rejected button, plus a bulk "Apply 30-day rule" button that sets every Applied job whose `date_applied` is at least `auto_reject_days` old and which has had no event since applying to Rejected, note `auto: no reply after 30 days`, one event each.
5. **Stage timeline:** detail-page buttons for Assessment, Interview, Final, Offer, Rejected, Withdrawn, No response, each with an optional note; append an event and update `status`/`last_update`. Timeline shown newest first.
6. **Dashboard:** this-week (Mon to today) applied vs `weekly_target` as a progress bar; total submitted, positive count, positive rate, and the rule check (fewer than `min_positive` per `per` submitted means "CV problem, not the market"; fewer than `per` submitted shows "too early"); applications per week for the last 12 weeks (Chart.js bar); funnel (submitted, positive, interview, offer); breakdown tables (submitted, positive, rate) by `cv_variant`, `source` and role type (backend, platform/devops/cloud, full-stack, AI/ML, graduate, other, derived from the title); Ready queue size and overdue follow-up count.
7. **Duplicate warning:** on add and on the detail page, a banner (never blocking) with a link when another row has the same normalised company (lowercase; strip `pty ltd`, `limited`, punctuation) and a role with `difflib` ratio at least 0.8 within the last 60 days.
8. **All applications:** free-text search (company, role, notes), filters for status, source, cv_variant and date range, sortable columns, result count. Includes the Add application form.

UI: top nav (Queue, Follow-ups, All, Dashboard), accessible, mobile-friendly, light/dark via `prefers-color-scheme`.

## 7. CLI and launching

- `jobtracker`: if `GET /health` answers on port 8765, open the browser and exit. Otherwise spawn a detached uvicorn (log: `~/Library/Logs/job-tracker.log`), wait for `/health`, open the browser, return to the prompt.
- `jobtracker stop`: confirms `/health` identifies Job Tracker, finds the listening process with `lsof -ti tcp:PORT -sTCP:LISTEN`, sends SIGTERM and waits for it to exit. uvicorn finishes the in-flight request first, so a write in progress completes. No pidfile is used, so a recycled PID can never be signalled.
- `jobtracker status`: reports running or not.
- `jobtracker install-launcher`: builds `~/Applications/Job Tracker.app` with `osacompile`, running the `jobtracker` binary by full path. Simple icon if easy, otherwise skipped.
- Installed globally with `uv tool install --editable .`.

## 8. Testing and verification

Unit tests (pytest, temp dirs): CSV round trip; unknown columns preserved; status change writes exactly one event; skip writes reason and event; 30-day rule at 29, 30 and 31 days and when already changed; duplicate detection (normalisation, ratio threshold, 60-day window); dashboard numbers on a small fixture with hand-computed expectations.

Concurrency test: a hook between the app's read and write lets a separate process append row B using the lock protocol while the app updates row A; both changes must survive.

Lifecycle tests: `jobtracker` when already running opens the browser and starts no second server; `jobtracker stop` stops the server and frees the port, including when issued during a write.

Corrupt-file test: a malformed CSV gives an error page and the file is byte-for-byte unchanged.

Then: run the full suite and show the output; start the app against a copy of the data folder (a copy of the agent's CSVs if they exist, otherwise a generated sample; never the real folder), walk through every feature, list what was clicked and seen, fix anything broken. Finish with a README: how to start (`jobtracker`, Spotlight), how to stop, where data lives, CSV schema, and how the scan should write (lock, re-read, append by id, atomic replace).

## 9. Out of scope

Idle auto-shutdown and heartbeat; login/auth; background services; any dependency beyond section 2; any Excel import or reading of the Excel file.
