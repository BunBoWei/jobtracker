"""HTTP routes. Pages are plain Jinja; buttons use HTMX and fall back to normal form posts."""
from __future__ import annotations

import html
import subprocess
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import service, stats
from .models import OTHER_REASON, SKIP_REASONS, STAGE_BUTTONS, STATUSES, find_duplicates, tailor_prefix

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
