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
