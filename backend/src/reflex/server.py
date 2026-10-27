"""HTTP layer. The ZIP arrives as the raw request body, is held in memory, and is never written to disk.

POST /api/upload            body = the ZIP bytes  ->  {"id": "..."}  or  {"error": {"code", "message"}}
GET  /api/jobs/{id}         ->  {"state": "running|done|error", "stage", "done", "total", "error"?}
GET  /api/jobs/{id}/report  ->  the report JSON, once; the job is deleted as it is handed over

Only the UI pages, the bundled JSZip and the clips folder are served. Nothing else in the project
folder (the backend, the takeouts folder, .env) is reachable.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from .config import get_setting, youtube_keys
from .enrich import rotating_fetcher
from .errors import ReflexError
from .ingest import MAX_ZIP_BYTES, open_takeout
from .pipeline import analyse
from . import taglines as tl
from .feedback import FeedbackStore, clean as clean_feedback

PAGES = {"index", "about", "preview", "how", "privacy", "upload", "report", "inspiration", "feedback", "contact"}
CLIP_SUFFIXES = {".mp4", ".webm"}
JOB_TTL_S = 15 * 60
MAX_RUNNING = 2

STATUS = {"notzip": 422, "nodata": 422, "html": 422, "big": 413, "lookup": 502}
MESSAGES = {
    "lookup": "I couldn't look up your videos on YouTube just now. Nothing was saved. Please try again later.",
    "busy": "A lot of people are reading their history right now. Try again in a minute.",
    "internal": "Something went wrong while reading your history. Nothing was saved.",
}


class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}

    def _purge(self) -> None:
        cutoff = time.time() - JOB_TTL_S
        for k in [k for k, j in self._jobs.items() if j["touched"] < cutoff]:
            del self._jobs[k]

    def running(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values() if j["state"] == "running")

    def create(self) -> str | None:
        with self._lock:
            self._purge()
            if sum(1 for j in self._jobs.values() if j["state"] == "running") >= MAX_RUNNING:
                return None
            jid = secrets.token_urlsafe(16)
            self._jobs[jid] = {"state": "running", "stage": "reading", "done": 0, "total": 0,
                               "error": None, "report": None, "touched": time.time()}
            return jid

    def update(self, jid: str, **fields) -> None:
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid].update(fields, touched=time.time())

    def status(self, jid: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                return None
            j["touched"] = time.time()
            out = {k: j[k] for k in ("state", "stage", "done", "total")}
            if j["error"]:
                out["error"] = j["error"]
            return out

    def take_report(self, jid: str) -> dict | None:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None or j["state"] != "done":
                return None
            del self._jobs[jid]
            return j["report"]


def _error(code: str, message: str, status: int | None = None) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status or STATUS.get(code, 400))


def create_app(*, cache_path: str, web_dir: str | Path,
               make_fetcher: Callable[[], object | None] | None = None, workers: int = 8,
               tagline_call: tl.Caller | None = None, tagline_limits: tl.Limits | None = None) -> FastAPI:
    web = Path(web_dir).resolve()
    jobs = Jobs()
    pool = ThreadPoolExecutor(max_workers=2)
    if make_fetcher is None:
        def make_fetcher():                                     # noqa: F811
            keys = youtube_keys()
            return rotating_fetcher(keys) if keys else None

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    cap = get_setting("TAGLINE_DAILY_CAP")
    limits = tagline_limits or tl.Limits(daily=int(cap) if cap and cap.isdigit() else 600)
    call = tagline_call or tl.gemini_call
    fb_store = FeedbackStore(str(Path(cache_path).resolve().with_name("feedback.sqlite")))
    fb_limits = tl.Limits(per_client=5, window_s=3600, daily=300)

    def run(jid: str, files) -> None:
        try:
            def progress(stage: str, done: int, total: int) -> None:
                jobs.update(jid, stage=stage, done=done, total=total)

            report = analyse(files, cache_path=cache_path, make_fetcher=make_fetcher,
                             progress=progress, workers=workers)
            jobs.update(jid, state="done", stage="done", report=report)
        except ReflexError as exc:
            msg = MESSAGES.get(exc.code, str(exc))
            jobs.update(jid, state="error", error={"code": exc.code, "message": msg})
        except Exception:                                       # never leak internals to the client
            jobs.update(jid, state="error", error={"code": "internal", "message": MESSAGES["internal"]})

    @app.post("/api/upload")
    async def upload(request: Request):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_ZIP_BYTES:
            return _error("big", "That file is too big.")
        if jobs.running() >= MAX_RUNNING:
            return _error("busy", MESSAGES["busy"], 503)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_ZIP_BYTES:
                return _error("big", "That file is too big.")
            chunks.append(chunk)
        data = b"".join(chunks)
        del chunks
        try:
            files = await run_in_threadpool(open_takeout, data)
        except ReflexError as exc:
            return _error(exc.code, str(exc) or exc.code)
        del data
        jid = jobs.create()
        if jid is None:
            return _error("busy", MESSAGES["busy"], 503)
        pool.submit(run, jid, files)
        return {"id": jid}

    @app.get("/api/jobs/{jid}")
    def job(jid: str):
        s = jobs.status(jid)
        return s if s is not None else _error("gone", "That job has expired.", 404)

    @app.get("/api/jobs/{jid}/report")
    def job_report(jid: str):
        r = jobs.take_report(jid)
        if r is None:
            return _error("gone", "That report is no longer here.", 404)
        return JSONResponse(r, headers={"Cache-Control": "no-store"})

    @app.post("/api/taglines")
    async def taglines(request: Request):
        """Body: {"facts": {...}, "avoid": [...], "tone": "roast"|...}. Only the cleaned fact sheet reaches the model."""
        raw = await request.body()
        if len(raw) > 8000:
            return _error("big", "That request is too big.", 413)
        try:
            body = json.loads(raw)
            facts = tl.clean_facts(body.get("facts"))
        except Exception:
            return _error("bad", "That request didn't look right.", 422)
        avoid = tl.clean_avoid(body.get("avoid"))
        tone = body.get("tone") if body.get("tone") in tl.TONES else None
        who = request.client.host if request.client else "?"
        if get_setting("TRUST_PROXY") and request.headers.get("x-forwarded-for"):
            who = request.headers["x-forwarded-for"].split(",")[0].strip()
        keys, models = tl.load_keys(), tl.load_models()
        if not keys or not limits.allow(who):
            return JSONResponse({"taglines": tl.fallback(facts), "source": "fallback"}, headers={"Cache-Control": "no-store"})
        out = await run_in_threadpool(tl.suggest, facts, avoid, tone, keys=keys, models=models, call=call)
        return JSONResponse(out, headers={"Cache-Control": "no-store"})

    @app.post("/api/feedback")
    async def feedback(request: Request):
        """Body: {"kinds"|"kind", "message", "rating"? 1-5, "contact"?, "website"? (honeypot)}. A rating alone is enough. Stored: kind, message, rating, contact, time. Never the IP."""
        raw = await request.body()
        if len(raw) > 6000:
            return _error("big", "That message is too long.", 413)
        try:
            body = json.loads(raw)
        except Exception:
            return _error("bad", "That didn't look right.", 422)
        if isinstance(body, dict) and body.get("website"):          # honeypot: pretend it worked
            return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})
        rec = clean_feedback(body)
        if rec is None:
            return _error("bad", "Pick a type and write a few words, or just leave a star rating.", 422)
        who = request.client.host if request.client else "?"
        if get_setting("TRUST_PROXY") and request.headers.get("x-forwarded-for"):
            who = request.headers["x-forwarded-for"].split(",")[0].strip()
        if not fb_limits.allow(who):
            return _error("rate", "That's a lot of feedback for one hour. Try again later.", 429)
        await run_in_threadpool(fb_store.add, rec)
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # ---- static: an explicit allow-list, never a directory mount ----
    @app.get("/")
    def root():
        return FileResponse(web / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/{name}.html")
    def page(name: str):
        if name not in PAGES or not (web / f"{name}.html").is_file():
            return _error("gone", "Not found.", 404)
        return FileResponse(web / f"{name}.html", headers={"Cache-Control": "no-cache"})

    @app.get("/jszip.min.js")
    def jszip():
        return FileResponse(web / "jszip.min.js")

    @app.get("/preview-card.webp")
    def preview_card():
        p = web / "preview-card.webp"
        if not p.is_file():
            return _error("gone", "Not found.", 404)
        return FileResponse(p, media_type="image/webp")

    @app.get("/videos/{filename}")
    def clip(filename: str):
        p = web / "videos" / filename
        if Path(filename).name != filename or p.suffix.lower() not in CLIP_SUFFIXES or not p.is_file():
            return _error("gone", "Not found.", 404)
        return FileResponse(p)

    return app
