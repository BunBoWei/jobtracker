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


class DataDirMissing(Exception):
    """The data folder and its parent are both missing (e.g. Google Drive is not mounted)."""

    def __init__(self, data_dir: Path):
        super().__init__(f"The data folder was not found: {data_dir}. Is Google Drive mounted and signed in? "
                         "Nothing was created.")
        self.data_dir = data_dir


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

    def _check_available(self) -> None:
        """First run is fine (folder missing, parent present). A missing parent means Drive is not mounted."""
        if not self.data_dir.exists() and not self.data_dir.parent.exists():
            raise DataDirMissing(self.data_dir)

    # ---- reads (no lock needed: writers replace files atomically) ----
    def read_apps(self) -> tuple[list[str], list[dict]]:
        self._check_available()
        header, rows = read_csv(self.apps_path, APP_COLUMNS)
        return _with_known_columns(header, rows, APP_COLUMNS), rows

    def read_events(self) -> tuple[list[str], list[dict]]:
        self._check_available()
        header, rows = read_csv(self.events_path, EVENT_COLUMNS)
        return _with_known_columns(header, rows, EVENT_COLUMNS), rows

    def get_app(self, app_id: str) -> dict:
        for r in self.read_apps()[1]:
            if r["id"] == app_id:
                return r
        raise AppNotFound(app_id)

    def settings(self) -> dict:
        self._check_available()
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
        if not path.exists():
            return ""
        with path.open(encoding="utf-8", newline="") as f:  # newline="" keeps CRLF as pasted
            return f.read()

    # ---- writes ----
    @contextmanager
    def locked(self):
        self._check_available()
        self.data_dir.mkdir(exist_ok=True)  # only the leaf: never invent a missing Drive path
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
