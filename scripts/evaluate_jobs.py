"""
evaluate_jobs.py — take un-evaluated jobs from the pool, run optional fit-scoring
and the 4-layer legitimacy filter, append to eval_log.csv, and send one email.
"""

import csv
import json
import time
from datetime import date
from pathlib import Path
import sys, os

os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, str(Path(__file__).parent))

from utils import match_jobs, send_email, canonical_url, load_config
import legitimacy

POOL_PATH     = Path("data/pool.json")
EVAL_LOG_PATH = Path("data/eval_log.csv")
LAST_RUN_PATH = Path("data/last_run.json")

EVAL_LOG_FIELDS = [
    "id", "evaluated_at", "direction", "source",
    "title", "company", "location", "url", "contact_email",
    "score", "score_source",
    "matched", "visa_ok", "visa_note",
    "reason", "research_fit", "deadline", "application_method",
    "security_flag", "security_note",
    # legitimacy filter (4-layer) ─ added columns, appended so old logs still load
    "legit_verdict", "legit_score", "legit_red_flags", "legit_green_flags",
]


def _legit_summary(result) -> dict:
    """Flatten a LegitimacyResult into eval_log columns + an email-friendly dict."""
    reds = "; ".join(f"{f.message} [{f.source}]".strip() for f in result.red_flags)
    greens = "; ".join(f.message for f in result.green_flags)
    return {
        "legit_verdict": result.verdict,
        "legit_score": round(result.score, 1),
        "legit_red_flags": reds,
        "legit_green_flags": greens,
    }

def load_evals() -> list[dict]:
    eval_dir = Path("evals")
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(eval_dir.glob("*.json"))
    ]


def _load_pool() -> dict:
    if POOL_PATH.exists():
        return json.loads(POOL_PATH.read_text())
    return {}


def _save_pool(pool: dict):
    POOL_PATH.write_text(json.dumps(pool, ensure_ascii=False, indent=2))


def _append_eval_log(rows: list[dict]):
    """Append rows to eval_log.csv, keeping the header consistent with EVAL_LOG_FIELDS.

    Writes a header when the file is missing or empty. If an existing header differs
    from EVAL_LOG_FIELDS (column count/names), rebuild the header before appending to
    avoid column misalignment.
    """
    needs_header = not EVAL_LOG_PATH.exists() or EVAL_LOG_PATH.stat().st_size == 0
    if not needs_header:
        with EVAL_LOG_PATH.open(encoding="utf-8") as f:
            existing_header = f.readline().strip().split(",")
        if existing_header != EVAL_LOG_FIELDS:
            # Header mismatch — rebuild with correct header preserving existing data
            import io
            with EVAL_LOG_PATH.open(encoding="utf-8") as f:
                old_rows = list(csv.DictReader(f))
            with EVAL_LOG_PATH.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=EVAL_LOG_FIELDS, extrasaction="ignore")
                w.writeheader()
                w.writerows(old_rows)
            print(f"  eval_log header fixed: {existing_header[:3]}... -> {EVAL_LOG_FIELDS[:3]}...")
    with EVAL_LOG_PATH.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVAL_LOG_FIELDS, extrasaction="ignore")
        if needs_header:
            writer.writeheader()
        writer.writerows(rows)


def evaluate_all():
    pool = _load_pool()
    today = date.today().isoformat()
    cfg = load_config()
    legit_cfg = dict(cfg.get("legitimacy", {}))
    # propagate the top-level remote_only flag into the legitimacy filter so it
    # can hard-veto non-remote postings when the user only wants remote roles.
    legit_cfg.setdefault("remote_only", cfg.get("remote_only", False))
    fit_cfg = cfg.get("fit_scoring", {})
    fit_enabled = fit_cfg.get("enabled", True)
    fit_min = fit_cfg.get("min_score", 6)
    legit_enabled = legit_cfg.get("enabled", True)
    push_verdicts = set(legit_cfg.get("push_verdicts", ["pass"]))

    all_matched: dict[str, list[dict]] = {}
    all_join_warnings: list[str] = []
    all_log_rows: list[dict] = []
    # candidates carry the full pool dict so the legitimacy filter has posting text
    legit_candidates: list[tuple[str, dict, dict]] = []  # (job_type, full_job, email_entry)

    def _entry_type(jdata: dict) -> str:
        # New pool entries carry `type`; anything else is treated as a generic job.
        return jdata.get("type") or "job"

    eval_stats: dict[str, dict] = {}  # {job_type: {pending, evaluated, fit_matched, pushed}}

    for eval_cfg in load_evals():
        job_type = eval_cfg["type"]
        pending = [
            {"id": jid, **jdata}
            for jid, jdata in pool.items()
            if not jdata.get("evaluated") and _entry_type(jdata) == job_type
        ]
        print(f"\n── {job_type} (pending {len(pending)}) ──")
        eval_stats[job_type] = {"pending": len(pending), "evaluated": 0,
                                "fit_matched": 0, "pushed": 0}
        if not pending:
            continue

        manual_scored = [j for j in pending if j.get("manual_score") is not None]
        auto_pending  = [j for j in pending if j.get("manual_score") is None]

        log_rows = []

        # manual scores: build eval_log rows directly, skip Claude
        if manual_scored:
            for job in manual_scored:
                log_rows.append({
                    "id":                 job["id"],
                    "evaluated_at":       today,
                    "direction":          job_type,
                    "source":             job.get("source", "manual"),
                    "title":              job.get("title", ""),
                    "company":            job.get("company", ""),
                    "location":           job.get("location", ""),
                    "url":                job.get("url", ""),
                    "contact_email":      job.get("contact_email", ""),
                    "score":              job["manual_score"],
                    "score_source":       "manual",
                    "matched":            True,
                    "visa_ok":            job.get("visa_ok", "yes"),
                    "visa_note":          job.get("visa_note", ""),
                    "reason":             "manually injected, pre-scored",
                    "research_fit":       "",
                    "deadline":           job.get("deadline", ""),
                    "application_method": job.get("application_method", "web"),
                    "security_flag":      False,
                    "security_note":      "",
                })
                if job["id"] in pool:
                    pool[job["id"]]["evaluated"] = True
                legit_candidates.append((job_type, job, {
                    "title":              job.get("title", ""),
                    "company":            job.get("company", ""),
                    "location":           job.get("location", ""),
                    "url":                job.get("url", ""),
                    "contact_email":      job.get("contact_email", ""),
                    "score":              job["manual_score"],
                    "score_source":       "manual",
                    "source":             "manual",
                    "visa_ok":            job.get("visa_ok", "yes"),
                    "visa_note":          job.get("visa_note", ""),
                    "reason":             "manually injected, pre-scored",
                    "deadline":           job.get("deadline", ""),
                    "application_method": job.get("application_method", "web"),
                }))
            print(f"  manual scores recorded: {len(manual_scored)}")

        # Claude fit-scoring (skippable: push purely on legitimacy)
        matched = []
        evaluated = []
        if auto_pending:
            if fit_enabled:
                all_claude, evaluated = match_jobs(auto_pending, eval_cfg)
                matched = [m for m in all_claude if float(m.get("score") or 0) >= fit_min]
            else:
                # No fit-scoring: every job is a candidate, legitimacy is the only gate.
                all_claude, evaluated = [], auto_pending

            # validate that Claude-returned URLs join back to a pool entry
            evaluated_canons = {canonical_url(j.get("url", "")) for j in evaluated}
            for cr in all_claude:
                if canonical_url(cr.get("url", "")) not in evaluated_canons:
                    all_join_warnings.append(
                        f"[{job_type}] {cr.get('title', '')} | {cr.get('url', '')}"
                    )

            for job in evaluated:
                job_canon = canonical_url(job.get("url", ""))
                claude_result = next(
                    (m for m in matched if canonical_url(m.get("url", "")) == job_canon), None
                )
                is_matched = claude_result is not None
                log_rows.append({
                    "id":                 job["id"],
                    "evaluated_at":       today,
                    "direction":          job_type,
                    "source":             job.get("source", ""),
                    "title":              job.get("title", ""),
                    "company":            job.get("company", ""),
                    "location":           claude_result.get("location", "") if claude_result else "",
                    "url":                job.get("url", ""),
                    "contact_email":      claude_result.get("contact_email", "") if claude_result else "",
                    "score":              claude_result.get("score", "") if claude_result else "",
                    "score_source":       "claude",
                    "matched":            is_matched,
                    "visa_ok":            claude_result.get("visa_ok", "") if claude_result else "",
                    "visa_note":          claude_result.get("visa_note", "") if claude_result else "",
                    "reason":             claude_result.get("reason", "") if claude_result else "",
                    "research_fit":       claude_result.get("research_fit", "") if claude_result else "",
                    "deadline":           claude_result.get("deadline", "") if claude_result else "",
                    "application_method": claude_result.get("application_method", "") if claude_result else "",
                    "security_flag":      claude_result.get("security_flag", False) if claude_result else False,
                    "security_note":      claude_result.get("security_note", "") if claude_result else "",
                })
                if job["id"] in pool:
                    pool[job["id"]]["evaluated"] = True

                # A job is a legitimacy candidate if it passed fit-scoring (or if
                # fit-scoring is off entirely). The full job dict carries the text
                # the filter needs; the email entry mirrors the matched-job shape.
                if (not fit_enabled) or is_matched:
                    cr = claude_result or {}
                    legit_candidates.append((job_type, job, {
                        "title":              job.get("title", ""),
                        "company":            job.get("company", ""),
                        "location":           cr.get("location", ""),
                        "url":                job.get("url", ""),
                        "contact_email":      cr.get("contact_email", ""),
                        "score":              cr.get("score", ""),
                        "score_source":       "claude",
                        "source":             job.get("source", ""),
                        "visa_ok":            cr.get("visa_ok", ""),
                        "visa_note":          cr.get("visa_note", ""),
                        "reason":             cr.get("reason", ""),
                        "deadline":           cr.get("deadline", ""),
                        "application_method": cr.get("application_method", ""),
                    }))

        # stash this type's log rows on the shared list, keyed by id for later
        # legitimacy back-fill before a single write at the end.
        all_log_rows.extend(log_rows)
        n_eval = len(evaluated) + len(manual_scored)
        eval_stats[job_type]["evaluated"] = n_eval
        eval_stats[job_type]["fit_matched"] = len(matched) + len(manual_scored)
        print(f"  fit-scored {len(evaluated)}, fit-matched {len(matched)}")

        if auto_pending and fit_enabled:
            time.sleep(15)

    # ── Legitimacy filter (4 layers) — only PASSing jobs get pushed ──────────
    log_by_id = {r["id"]: r for r in all_log_rows}
    if legit_enabled and legit_candidates:
        print(f"\n── Legitimacy filter: {len(legit_candidates)} candidate(s) ──")
        jobs_for_legit = [job for _, job, _ in legit_candidates]
        results = legitimacy.assess_batch(jobs_for_legit, cfg=legit_cfg)
        for (job_type, job, email_entry), result in zip(legit_candidates, results):
            summary = _legit_summary(result)
            if job["id"] in log_by_id:
                log_by_id[job["id"]].update(summary)
            email_entry.update(summary)
            eval_stats.setdefault(job_type, {}).setdefault("pushed", 0)
            if result.verdict in push_verdicts:
                all_matched.setdefault(job_type, []).append(email_entry)
                eval_stats[job_type]["pushed"] += 1
            print(f"  [{result.verdict:6s} {result.score:4.1f}] "
                  f"{job.get('title','')[:50]} @ {job.get('company','')[:30]}")
    else:
        # legitimacy disabled — push everything that was a candidate
        for job_type, _job, email_entry in legit_candidates:
            all_matched.setdefault(job_type, []).append(email_entry)
            eval_stats.setdefault(job_type, {})["pushed"] = \
                eval_stats.get(job_type, {}).get("pushed", 0) + 1

    _append_eval_log(all_log_rows)
    _save_pool(pool)

    run_data = {}
    if LAST_RUN_PATH.exists():
        run_data = json.loads(LAST_RUN_PATH.read_text())
    run_data["evaluated_at"] = today
    run_data["url_join_warnings"] = all_join_warnings
    LAST_RUN_PATH.write_text(json.dumps(run_data, ensure_ascii=False, indent=2))

    send_email(all_matched, warnings=all_join_warnings,
               run_stats=run_data.get("searches", {}), eval_stats=eval_stats)

    return all_matched


if __name__ == "__main__":
    evaluate_all()
