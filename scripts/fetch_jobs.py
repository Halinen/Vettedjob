"""
fetch_jobs.py — scan searches/*.json, fetch every source, update pool.json.
Dedup by: all IDs in pool.json + all IDs in eval_log.csv (derived at runtime).
Entries older than 30 days (and evaluated) are cleaned up automatically.
"""

import json
import sys
import os
from datetime import date, timedelta
from pathlib import Path

os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, str(Path(__file__).parent))

from sources import SOURCE_REGISTRY
from utils import build_seen_ids, canonical_url

POOL_PATH         = Path("data/pool.json")
INJECT_QUEUE_PATH = Path("data/inject_queue.json")
LAST_RUN_PATH     = Path("data/last_run.json")
TTL_DAYS = 30


def _load_pool() -> dict:
    if POOL_PATH.exists():
        return json.loads(POOL_PATH.read_text())
    return {}


def _save_pool(pool: dict):
    POOL_PATH.write_text(json.dumps(pool, ensure_ascii=False, indent=2))


def _cleanup_pool(pool: dict) -> dict:
    """Delete evaluated entries older than TTL_DAYS; un-evaluated get a grace period
    up to TTL_DAYS + 7."""
    today = date.today()
    to_delete = []
    for job_id, job in pool.items():
        fetched = date.fromisoformat(job.get("fetched_at", "2000-01-01"))
        age = (today - fetched).days
        if job.get("evaluated") and age > TTL_DAYS:
            to_delete.append(job_id)
        elif not job.get("evaluated") and age > TTL_DAYS + 7:
            to_delete.append(job_id)
    for job_id in to_delete:
        del pool[job_id]
    if to_delete:
        print(f"  cleaned up expired entries: {len(to_delete)}")
    return pool


def _normalize_title(t: str) -> str:
    import re as _re
    return _re.sub(r'\s+', ' ', t.lower().strip())


def load_searches() -> list[dict]:
    search_dir = Path("searches")
    return [
        json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(search_dir.glob("*.json"))
    ]


def fetch_all() -> dict:
    """Scan searches/*.json, fetch all sources, return the updated pool."""
    pool = _load_pool()
    seen = build_seen_ids()
    today = date.today().isoformat()
    search_stats = {}

    title_seen = {
        (_normalize_title(j.get("title", "")),
         j.get("company", "").lower().strip(),
         canonical_url(j.get("url", "")))
        for j in pool.values()
    }

    for search in load_searches():
        job_type = search["type"]
        for src_cfg in search["sources"]:
            src_id = src_cfg["id"]
            fn = SOURCE_REGISTRY[src_cfg["source"]]
            inc = src_cfg["include"] if "include" in src_cfg else search.get("include", [])
            exc = src_cfg["exclude"] if "exclude" in src_cfg else search.get("exclude", [])
            kwargs = {"include": inc, "exclude": exc}
            if "max_results" in src_cfg:
                kwargs["max_results"] = src_cfg["max_results"]

            try:
                jobs, stats = fn(**kwargs)
            except Exception as e:
                print(f"  [{src_id}] source failed: {e}")
                continue

            new_count = 0
            for job in jobs:
                if job["id"] in seen:
                    continue
                title_key = (_normalize_title(job.get("title", "")),
                             job.get("company", "").lower().strip(),
                             canonical_url(job.get("url", "")))
                if title_key in title_seen:
                    continue
                pool[job["id"]] = {
                    "title":       job.get("title", ""),
                    "company":     job.get("company", ""),
                    "url":         job.get("url", ""),
                    "description": job.get("description", ""),
                    "type":        job_type,
                    "source":      src_id,
                    "fetched_at":  today,
                    "evaluated":   False,
                }
                seen.add(job["id"])
                title_seen.add(title_key)
                new_count += 1

            search_stats[src_id] = {**stats, "new_to_pool": new_count}
            print(f"  [{src_id}] fetched {stats['fetched']} after-include {stats['after_include']} after-exclude {stats['after_exclude']} new-to-pool {new_count}")

    pool = _process_inject_queue(pool, seen)
    pool = _cleanup_pool(pool)
    _save_pool(pool)

    LAST_RUN_PATH.write_text(json.dumps(
        {"run_at": today, "searches": search_stats},
        ensure_ascii=False, indent=2
    ))

    pending = sum(1 for j in pool.values() if not j["evaluated"])
    print(f"  Pool total: {len(pool)} (pending {pending})")
    return pool


def _process_inject_queue(pool: dict, seen: set) -> dict:
    """Read inject_queue.json and merge unprocessed entries into the pool."""
    if not INJECT_QUEUE_PATH.exists():
        return pool
    queue = json.loads(INJECT_QUEUE_PATH.read_text(encoding="utf-8"))
    processed_count = 0
    for item in queue:
        if item.get("processed"):
            continue
        if item["id"] in seen:
            item["processed"] = True
            continue
        pool[item["id"]] = {
            "title":              item.get("title", ""),
            "company":            item.get("company", ""),
            "url":                item.get("url", ""),
            "description":        item.get("description", ""),
            "type":               item.get("type", "job"),
            "source":             "manual",
            "fetched_at":         date.today().isoformat(),
            "evaluated":          False,
            "manual_score":       item.get("manual_score"),
            "contact_email":      item.get("contact_email", ""),
            "application_method": item.get("application_method", "web"),
            "deadline":           item.get("deadline", ""),
            "location":           item.get("location", ""),
        }
        seen.add(item["id"])
        item["processed"] = True
        processed_count += 1
    INJECT_QUEUE_PATH.write_text(
        json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    if processed_count:
        print(f"  inject_queue: merged {processed_count} manual job(s)")
    return pool


if __name__ == "__main__":
    fetch_all()
