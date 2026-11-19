"""Constants and pure helpers shared by every other module. No I/O."""
from __future__ import annotations

import re
from datetime import date
from difflib import SequenceMatcher

APP_COLUMNS = [
    "id", "status", "date_applied", "company", "role", "level", "location",
    "work_mode", "job_type", "source", "job_link", "job_id", "cv_variant", "cv_file",
    "priority", "match_reason", "next_action", "next_action_date", "contact",
    "salary", "skip_reason", "notes", "created_at", "last_update",
]
EVENT_COLUMNS = ["id", "application_id", "date", "from_status", "to_status", "note"]

STATUSES = [
    "Ready", "Applied", "Assessment", "Interview", "Final", "Offer",
    "Rejected", "No response", "Withdrawn", "Skipped", "Incomplete",
]
JOB_TYPES = ["Full-time", "Part-time", "Casual", "Contract"]
DEFAULT_JOB_TYPE = "Full-time"  # a blank job_type (older rows, scan rows) counts as full-time

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
    "work_mode", "source", "match_reason", "job_type",
)

# Region buckets worked out from the free-text location (first match wins, so Remote beats a city).
REGIONS = ["Wollongong/Illawarra", "Sydney", "Remote", "Other NSW", "Interstate", "Other", "Unknown"]
_REGION_RULES = [
    ("Remote", r"\bremote\b|work from home|\bwfh\b"),
    ("Wollongong/Illawarra", r"wollongong|illawarra|shellharbour|kiama|dapto|corrimal|thirroul|fairy meadow|"
                             r"unanderra|warrawong|figtree|albion park|bulli|port kembla|keiraville|gwynneville"),
    ("Sydney", r"sydney|parramatta|chatswood|macquarie park|north ryde|\bryde\b|surry hills|ultimo|pyrmont|"
               r"barangaroo|haymarket|mascot|alexandria|st leonards|norwest|bella vista|liverpool|penrith|"
               r"blacktown|hornsby|bondi|newtown|redfern|rhodes|olympic park|sutherland|miranda|campbelltown"),
    ("Other NSW", r"\bnsw\b|new south wales|newcastle|central coast|albury|wagga|dubbo|port macquarie|"
                  r"coffs harbour|nowra"),
    ("Interstate", r"melbourne|brisbane|perth|adelaide|canberra|hobart|darwin|gold coast|sunshine coast|"
                   r"geelong|victoria|queensland|\b(vic|qld|wa|sa|act|tas|nt)\b"),
]


def region_of(location: str) -> str:
    text = (location or "").strip().lower()
    if not text:
        return "Unknown"
    for name, pattern in _REGION_RULES:
        if re.search(pattern, text):
            return name
    return "Other"


def job_type_of(row: dict) -> str:
    value = (row.get("job_type") or "").strip()
    return value if value in JOB_TYPES else DEFAULT_JOB_TYPE


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
