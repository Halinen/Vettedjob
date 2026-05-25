from __future__ import annotations

import hashlib
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import legitimacy
from sources import fetch_jobspy
from utils import canonical_url, load_config, match_jobs


WEB_ROOT = REPO_ROOT / "web"
STATIC_ROOT = WEB_ROOT / "static"
RESUME_TMP_DIR = WEB_ROOT / "_tmp_resumes"
RESUME_TMP_DIR.mkdir(parents=True, exist_ok=True)

WEB_MAX_FETCH = int(os.environ.get("WEB_MAX_FETCH", "15"))
# How many jobs to legitimacy-assess in parallel. Each assess() makes blocking
# Claude calls (Layer 2 web search is the slow part), so threads overlap the waits.
# Kept modest to avoid Anthropic rate limits.
WEB_ASSESS_CONCURRENCY = int(os.environ.get("WEB_ASSESS_CONCURRENCY", "5"))
TASKS: dict[str, dict[str, Any]] = {}
RESUME_TMP: dict[str, str] = {}
FETCH_LOCK = Lock()

app = FastAPI(title="Job Search Toolkit Web")
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


class CheckRequest(BaseModel):
    title: str = Field(default="", max_length=300)
    company: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=12000)
    url: str = Field(default="", max_length=2000)
    contact_email: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=300)


class FetchRequest(BaseModel):
    keywords: str = Field(..., min_length=2, max_length=300)
    exclude: str = Field(default="", max_length=600)
    country: str = Field(default="usa", max_length=80)
    location: str = Field(default="", max_length=120)
    remote_only: bool = False
    max_results: int = Field(default=10, ge=1, le=100)
    verification_mode: str = Field(default="fast", pattern="^(fast|full)$")
    fit_scoring: bool = False
    resume_token: str | None = None


@app.get("/")
def index():
    return FileResponse(STATIC_ROOT / "index.html")


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "has_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "web_max_fetch": WEB_MAX_FETCH,
    }


@app.post("/api/check")
def check_job(req: CheckRequest):
    if not req.title.strip() or not req.description.strip():
        raise HTTPException(status_code=422, detail="title and description are required")

    job = _request_to_job(req)
    result = legitimacy.assess(job, cfg=_legit_cfg())
    return result.to_dict()


@app.post("/api/resume")
async def upload_resume(
    text: str | None = Form(default=None),
    file: UploadFile | None = File(default=None),
):
    content = (text or "").strip()
    if file is not None and file.filename:
        raw = await file.read()
        content = _extract_resume_text(file.filename, raw)

    if not content.strip():
        raise HTTPException(status_code=422, detail="paste text or upload a .txt, .md, or .pdf")

    token = f"tmp_{uuid.uuid4().hex}"
    path = RESUME_TMP_DIR / f"{token}.md"
    path.write_text(content, encoding="utf-8")
    RESUME_TMP[token] = str(path)
    return {"resume_token": token, "chars": len(content)}


@app.post("/api/fetch", status_code=202)
def start_fetch(req: FetchRequest, background_tasks: BackgroundTasks):
    if not FETCH_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=429, detail="another fetch task is already running")

    task_id = uuid.uuid4().hex
    TASKS[task_id] = {
        "status": "running",
        "progress": {"phase": "queued", "done": 0, "total": 0},
        "result": None,
        "error": None,
    }
    background_tasks.add_task(run_fetch_task, task_id, req)
    return {"task_id": task_id, "status": "running"}


@app.get("/api/fetch/{task_id}")
def fetch_status(task_id: str):
    task = TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="unknown task_id")
    if task["status"] == "done":
        return {"status": "done", **task["result"]}
    if task["status"] == "error":
        return {"status": "error", "error": task["error"]}
    return {"status": "running", "progress": task["progress"]}


def run_fetch_task(task_id: str, req: FetchRequest):
    try:
        _set_progress(task_id, "fetching", 0, 0)
        capped = max(1, min(req.max_results, WEB_MAX_FETCH))
        jobs, stats = _fetch_with_override(req, capped)
        _set_progress(task_id, "fetched", len(jobs), len(jobs))

        fit_by_url: dict[str, dict[str, Any]] = {}
        if req.fit_scoring:
            fit_by_url = _score_fit(task_id, req, jobs)

        total = len(jobs)
        results: list[Any] = [None] * total
        cfg = _legit_cfg()
        _set_progress(task_id, "assessing", 0, total)

        def _assess_one(i: int, job: dict[str, Any]):
            # assess() is self-throttling internally only in assess_batch; here each
            # job runs independently in its own thread, no inter-job sleep needed.
            legit = _assess_for_mode(job, cfg, req.verification_mode)
            fit = fit_by_url.get(canonical_url(job.get("url", "")))
            return i, _result_row(job, legit, fit)

        done = 0
        workers = max(1, min(WEB_ASSESS_CONCURRENCY, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_assess_one, i, job) for i, job in enumerate(jobs)]
            for fut in as_completed(futures):
                i, row = fut.result()
                results[i] = row
                done += 1
                _set_progress(task_id, "assessing", done, total)

        _set_progress(task_id, "done", total, total)
        TASKS[task_id]["status"] = "done"
        TASKS[task_id]["result"] = {"stats": stats, "results": results}
    except Exception as exc:
        TASKS[task_id]["status"] = "error"
        TASKS[task_id]["error"] = str(exc)
    finally:
        FETCH_LOCK.release()


def _request_to_job(req: CheckRequest) -> dict[str, Any]:
    identity = req.url.strip() or f"{req.title}|{req.company}"
    digest = hashlib.md5(identity.encode("utf-8")).hexdigest()[:12]
    return {
        "id": f"web_{digest}",
        "title": req.title.strip(),
        "company": req.company.strip(),
        "description": req.description.strip(),
        "url": req.url.strip(),
        "contact_email": req.contact_email.strip(),
        "location": req.location.strip(),
    }


def _legit_cfg() -> dict[str, Any]:
    cfg = load_config()
    legit_cfg = dict(cfg.get("legitimacy", {}))
    legit_cfg.setdefault("remote_only", cfg.get("remote_only", False))
    return legit_cfg


def _fetch_with_override(req: FetchRequest, capped: int):
    cfg = load_config()
    previous_remote = cfg.get("remote_only", False)
    previous_jobspy = dict(cfg.get("jobspy", {}))
    try:
        cfg["remote_only"] = bool(req.remote_only)
        cfg.setdefault("jobspy", {}).update({
            "country": req.country.strip() or "usa",
            "location": req.location.strip(),
        })
        include = req.keywords.split()
        exclude = [item.strip() for item in req.exclude.split(",") if item.strip()]
        return fetch_jobspy(include=include, exclude=exclude, max_results=capped)
    finally:
        cfg["remote_only"] = previous_remote
        cfg["jobspy"] = previous_jobspy


def _score_fit(task_id: str, req: FetchRequest, jobs: list[dict[str, Any]]):
    token = req.resume_token or ""
    profile_path = RESUME_TMP.get(token)
    if not profile_path:
        raise HTTPException(status_code=422, detail="fit scoring requires a valid resume_token")

    _set_progress(task_id, "fit_scoring", 0, len(jobs))
    eval_cfg = json.loads(Path("evals/job.json").read_text(encoding="utf-8"))
    eval_cfg["profile"] = profile_path
    scored, _evaluated = match_jobs(jobs, eval_cfg)
    fit_min = load_config().get("fit_scoring", {}).get("min_score", 6)

    fit_by_url = {}
    for item in scored:
        url = canonical_url(item.get("url", ""))
        if not url:
            continue
        try:
            score = float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        fit_by_url[url] = {
            "score": item.get("score"),
            "reason": item.get("reason", ""),
            "research_fit": item.get("research_fit", ""),
            "matched": score >= fit_min,
        }
    return fit_by_url


def _assess_for_mode(job: dict[str, Any], cfg: dict[str, Any], mode: str):
    if mode == "full":
        return legitimacy.assess(job, cfg=cfg)

    l1_flags, l1_raw = legitimacy.layer1_rules(job, cfg)
    hard_veto_codes = {"upfront_fee", "salary_anomaly", "not_remote"}
    has_hard_veto = any(
        flag.severity == "red" and flag.code in hard_veto_codes for flag in l1_flags
    )
    score = legitimacy.HARD_FLAG_SCORE_CAP if has_hard_veto else 6.0
    # Fast mode intentionally avoids claiming a verified PASS. Clean-looking jobs
    # stay REVIEW until the user opts into full web/LLM verification.
    result = legitimacy.layer4_aggregate(
        job.get("id", ""),
        l1_flags,
        score,
        [],
        [],
        cfg={**cfg, "pass_threshold": 99},
    )
    result.layers = {
        "layer1": l1_raw,
        "layer2": {"skipped": "fast_fetch_mode"},
        "layer3": {"skipped": "fast_fetch_mode"},
    }
    return result


def _result_row(job: dict[str, Any], legit, fit: dict[str, Any] | None):
    data = legit.to_dict()
    return {
        "job_id": job.get("id", data.get("job_id", "")),
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "url": job.get("url", ""),
        "location": job.get("location", ""),
        "legit": {
            "verdict": data["verdict"],
            "score": data["score"],
            "flags": data["flags"],
        },
        "fit": fit,
    }


def _extract_resume_text(filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return raw.decode("utf-8", errors="replace").strip()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise HTTPException(status_code=500, detail="pypdf is not installed") from exc
        reader = PdfReader(BytesIO(raw))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    raise HTTPException(status_code=422, detail="supported resume formats: .txt, .md, .pdf")


def _set_progress(task_id: str, phase: str, done: int, total: int):
    TASKS[task_id]["progress"] = {"phase": phase, "done": done, "total": total}
