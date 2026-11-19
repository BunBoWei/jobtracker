"""HTTP routes. Pages are plain Jinja; buttons use HTMX and fall back to normal form posts."""
from __future__ import annotations

import html
import re
import subprocess
from pathlib import Path

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from . import service, stats
from .models import (DEFAULT_JOB_TYPE, EDITABLE_FIELDS, JOB_TYPES, OTHER_REASON, REGIONS, SKIP_REASONS, STAGE_BUTTONS,
                     STATUSES, find_duplicates, job_type_of, region_of, tailor_prefix)

router = APIRouter()
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}
CV_SUFFIXES = {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".pages"}


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


def _back(path: str) -> str:
    """Where to return after a form post. Only '/' or one of our job pages: never an arbitrary URL."""
    return path if re.fullmatch(r"/a/[A-Za-z0-9_-]+", path or "") else "/"


def _store(request: Request):
    return request.app.state.store


@router.get("/health")
def health():
    return {"app": "job-tracker", "ok": True}


@router.get("/")
def queue(request: Request, region: str = "", job_type: str = ""):
    store = _store(request)
    _, rows = store.read_apps()
    ready = sorted((r for r in rows if r["status"] == "Ready"
                    and (not region or region_of(r.get("location", "")) == region)
                    and (not job_type or job_type_of(r) == job_type)),
                   key=lambda r: (r["created_at"], r["id"]), reverse=True)
    groups: dict[str, list[dict]] = {}
    for r in ready:
        groups.setdefault(r["priority"].strip() or "Unprioritised", []).append(r)
    ordered = sorted(groups.items(), key=lambda kv: (PRIORITY_RANK.get(kv[0].lower(), 3), kv[0]))
    return render(request, "queue.html", active="queue", groups=ordered, total=len(ready),
                  tally=stats.skip_tally(rows, store.today()), skip_reasons=SKIP_REASONS,
                  regions=REGIONS, job_types=JOB_TYPES, f={"region": region, "job_type": job_type})


@router.post("/a/{app_id}/apply")
def apply(request: Request, app_id: str, ad_text: str = Form(""), back: str = Form("/")):
    service.mark_applied(_store(request), app_id, ad_text)
    return reply(request, "", _back(back))


@router.post("/a/{app_id}/skip")
def skip(request: Request, app_id: str, reason: str = Form(...), other_text: str = Form(""), back: str = Form("/")):
    if reason == OTHER_REASON:
        reason = other_text.strip()
        if not reason:
            raise ValueError("Type a reason for 'other'.")
    elif reason not in SKIP_REASONS:
        raise ValueError("Unknown skip reason.")
    service.skip(_store(request), app_id, reason)
    return reply(request, "", _back(back))


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
    if path.suffix.lower() not in CV_SUFFIXES:  # `open` would run scripts and apps
        return HTMLResponse("Only document files (pdf, doc, docx, txt, md, rtf, odt, pages) can be opened here.", status_code=400)
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
        stage_buttons=STAGE_BUTTONS, skip_reasons=SKIP_REASONS, job_types=JOB_TYPES, job_type=job_type_of(row),
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
async def save_details(request: Request, app_id: str):
    """Save only the fields the user actually changed, so a scan edit made meanwhile is not reverted."""
    form = await request.form()
    norm = lambda v: (v or "").replace("\r\n", "\n")
    fields = {k: form[k] for k in EDITABLE_FIELDS if k in form and norm(form[k]) != norm(form.get(f"orig_{k}"))}
    if fields.get("job_type", JOB_TYPES[0]) not in JOB_TYPES:
        fields.pop("job_type")
    await run_in_threadpool(service.update_details, _store(request), app_id, fields)
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
    service.change_status(_store(request), app_id, "Rejected", "no reply", expect={"Applied"})
    return reply(request, "", "/followups")


@router.post("/followups/apply-rule")
def apply_rule(request: Request):
    ids = service.apply_rejection_rule(_store(request))
    return RedirectResponse(f"/followups?rejected={len(ids)}", status_code=303)


@router.get("/all")
def all_page(
    request: Request, q: str = "", status: str = "", source: str = "", cv_variant: str = "",
    date_from: str = "", date_to: str = "", sort: str = "last_update", direction: str = Query("desc", alias="dir"),
    region: str = "", job_type: list[str] = Query(default=[]),
):
    _, rows = _store(request).read_apps()
    picked = [t for t in job_type if t in JOB_TYPES]
    found = stats.filter_rows(rows, q, status, source, cv_variant, date_from, date_to, sort, direction,
                              region=region, job_types=picked or None)
    current = {"q": q, "status": status, "source": source, "cv_variant": cv_variant, "date_from": date_from,
               "date_to": date_to, "region": region, "job_type": picked}
    current = {k: v for k, v in current.items() if v}
    sort_urls = {
        col: "/all?" + urlencode({**current, "sort": col, "dir": "asc" if (sort == col and direction == "desc") else "desc"},
                                 doseq=True)
        for col in stats.SORTABLE
    }
    return render(
        request, "all.html", active="all", rows=found, f=current, sort=sort, direction=direction, sort_urls=sort_urls,
        statuses=STATUSES, sources=sorted({r["source"] for r in rows if r["source"]}),
        cvs=sorted({r["cv_variant"] for r in rows if r["cv_variant"]}),
        regions=REGIONS, job_types=JOB_TYPES, region_of=region_of, job_type_of=job_type_of,
    )


@router.get("/add")
def add_form(request: Request):
    return render(request, "add.html", statuses=["Ready", "Applied", "Incomplete"], job_types=JOB_TYPES,
                  default_job_type=DEFAULT_JOB_TYPE)


@router.post("/add")
def add(
    request: Request, company: str = Form(""), role: str = Form(""), status: str = Form("Ready"),
    location: str = Form(""), source: str = Form(""), job_link: str = Form(""), cv_variant: str = Form(""),
    cv_file: str = Form(""), notes: str = Form(""), job_type: str = Form(DEFAULT_JOB_TYPE),
):
    app_id = service.add_application(_store(request), dict(
        company=company, role=role, status=status, location=location, source=source,
        job_link=job_link, cv_variant=cv_variant, cv_file=cv_file, notes=notes,
        job_type=job_type if job_type in JOB_TYPES else DEFAULT_JOB_TYPE))
    return RedirectResponse(_detail_url(app_id), status_code=303)


@router.get("/dashboard")
def dashboard(request: Request, job_type: list[str] = Query(default=[])):
    """Counts only the ticked types of work. Nothing ticked (first visit) means full-time only."""
    store = _store(request)
    _, rows = store.read_apps()
    selected = [t for t in JOB_TYPES if t in job_type] or [DEFAULT_JOB_TYPE]
    return render(request, "dashboard.html", active="dashboard", job_types=JOB_TYPES, selected=selected,
                  d=stats.dashboard(rows, store.settings(), store.today(), job_types=selected))
