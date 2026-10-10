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
