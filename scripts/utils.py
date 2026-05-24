import os
import json
import csv
import random
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

MAX_JOBS_PER_CALL = 40

_client = None
_config = None


def load_config(path: str = "config.json") -> dict:
    """Load project config.json once and cache it. Returns {} if absent."""
    global _config
    if _config is None:
        p = Path(path)
        _config = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    return _config


def get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client


_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "tracking", "source", "mc_cid", "mc_eid",
}


def canonical_url(url: str) -> str:
    """Normalize a URL: lowercase, drop tracking params + fragment, keep business
    params, strip trailing slash."""
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
    parsed = urlparse(url.lower().strip())
    filtered_qs = urlencode([
        (k, v) for k, v in parse_qsl(parsed.query)
        if k not in _TRACKING_PARAMS
    ])
    normalized = parsed._replace(query=filtered_qs, fragment="")
    return urlunparse(normalized).rstrip("/")



def build_seen_ids(pool_path: str = "data/pool.json",
                   eval_log_path: str = "data/eval_log.csv") -> set:
    """Derive the dedup set at runtime: all IDs in the pool + all IDs in eval_log."""
    seen = set()
    p = Path(pool_path)
    if p.exists():
        seen.update(json.loads(p.read_text()).keys())
    e = Path(eval_log_path)
    if e.exists():
        with e.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("id"):
                    seen.add(row["id"])
    return seen


def match_jobs(jobs: list[dict], eval_cfg: dict) -> tuple[list[dict], list[dict]]:
    """
    Returns (matched, evaluated):
      matched   — jobs scored >= 6 by Claude
      evaluated — jobs actually sent to Claude (used to mark pool evaluated=true)

    eval_cfg fields:
      model, max_tokens, max_per_call, profile, system_prompt, shared
    """
    if not jobs:
        return [], []

    max_per_call = eval_cfg.get("max_per_call", MAX_JOBS_PER_CALL)
    if len(jobs) > max_per_call:
        print(f"  {len(jobs)} jobs exceeds the cap; sampling {max_per_call} this run, rest deferred")
        jobs = random.sample(jobs, max_per_call)

    # build the system prompt (static: rules + candidate background)
    profile = Path(eval_cfg["profile"]).read_text(encoding="utf-8")
    system_template = Path(eval_cfg["system_prompt"]).read_text(encoding="utf-8")
    shared_path = Path(eval_cfg.get("shared", "data/prompts/_shared.md"))
    shared_content = shared_path.read_text(encoding="utf-8") if shared_path.exists() else ""
    system_prompt = (system_template
                     .replace("{profile}", profile)
                     .replace("{shared}", shared_content))

    # build the user message (dynamic: this batch of jobs)
    jobs_text = "\n\n".join(
        f"[{i+1}] {j['title']} @ {j['company']}\nURL: {j['url']}\n{j['description']}"
        for i, j in enumerate(jobs)
    )

    model = eval_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = eval_cfg.get("max_tokens", 2000)

    for attempt in range(3):
        try:
            response = get_client().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": jobs_text}]
            )
            text = response.content[0].text.strip()
            start = text.find("[")
            end = text.rfind("]") + 1
            if start == -1 or end == 0:
                print("  Claude did not return a JSON array")
                return [], jobs
            return json.loads(text[start:end]), jobs
        except Exception as e:
            if "rate_limit" in str(e).lower() and attempt < 2:
                wait = 60 * (attempt + 1)
                print(f"  Rate limit; retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"  Claude call failed: {e}")
                return [], []
    return [], []


def send_email(all_matched: dict[str, list[dict]],
               warnings: list[str] | None = None,
               run_stats: dict | None = None,
               eval_stats: dict | None = None):
    """
    Send one combined email with all pushed jobs, whether or not there are any.
    all_matched  — {direction: [job, ...]}
    warnings     — list of URL-join failures
    run_stats    — the `searches` field from last_run.json (per-source fetch stats)
    eval_stats   — {job_type: {pending, evaluated, fit_matched, pushed}}
    """
    import smtplib
    from email.mime.text import MIMEText
    from datetime import date

    total = sum(len(v) for v in all_matched.values())

    # ── run summary ──────────────────────────────────────
    body = "[Today's run]\n"

    if eval_stats:
        for jtype, s in eval_stats.items():
            body += (f"  {jtype:8s}  pending {s.get('pending', 0):3d}  ->  "
                     f"evaluated {s.get('evaluated', 0):3d}  ->  "
                     f"fit-matched {s.get('fit_matched', 0):2d}  ->  "
                     f"verified & pushed {s.get('pushed', 0):2d}\n")
    body += "\n"

    if run_stats:
        total_fetched  = sum(s.get("fetched", 0)   for s in run_stats.values())
        total_new      = sum(s.get("new_to_pool", 0) for s in run_stats.values())
        body += f"Sources fetched {total_fetched}, {total_new} new to pool\n"
        new_sources = [(sid, s["new_to_pool"]) for sid, s in run_stats.items() if s.get("new_to_pool", 0) > 0]
        if new_sources:
            for sid, n in new_sources:
                body += f"  + {sid}: {n} new\n"
        body += "\n"

    if warnings:
        body += f"WARNING: {len(warnings)} URL-join failures (may be mis-marked unmatched):\n"
        for w in warnings:
            body += f"  - {w}\n"
        body += "\n"

    body += "=" * 50 + "\n\n"

    # ── verified job list ─────────────────────────────────
    if total == 0:
        body += "No verified jobs to recommend today.\n"
    else:
        body += f"{total} verified job(s) today (passed the legitimacy filter):\n\n"
        for direction, jobs in all_matched.items():
            if not jobs:
                continue
            body += f"=== {direction} ({len(jobs)}) ===\n\n"
            for job in jobs:
                company = job.get("company", "")
                legit = job.get("legit_verdict", "")
                lscore = job.get("legit_score", "")
                fit = job.get("score", "?")
                header = f"[fit {fit}/10]"
                if legit:
                    header += f" [legit {legit.upper()} {lscore}/10]"
                body += f"{header} {job.get('title', '')} @ {company}\n"
                if job.get("reason"):
                    body += f"  {job['reason']}\n"
                greens = job.get("legit_green_flags", "")
                reds = job.get("legit_red_flags", "")
                if greens:
                    body += f"  + {greens}\n"
                if reds:
                    body += f"  ! {reds}\n"
                if job.get("deadline"):
                    body += f"  deadline: {job['deadline']}\n"
                body += f"  {job.get('url', '')}\n\n"

    today = date.today()
    subject = (f"[Job filter] {total} verified job(s) ({today})"
               if total > 0 else f"[Job filter] no verified jobs ({today})")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = os.environ["EMAIL_FROM"]
    msg["To"] = os.environ["EMAIL_TO"]

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(os.environ["EMAIL_FROM"], os.environ["EMAIL_APP_PASSWORD"])
        server.send_message(msg)
    print(f"  email sent: {total} recommended job(s)")
