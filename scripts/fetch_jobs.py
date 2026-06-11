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
from utils import build_seen_ids, canonical_url, load_config, looks_remote

POOL_PATH         = Path("data/pool.json")
INJECT_QUEUE_PATH = Path("data/inject_queue.json")
LAST_RUN_PATH     = Path("data/last_run.json")
TTL_DAYS = 30


def _load_pool() -> dict:
    if POOL_PATH.exists():
        return json.loads(POOL_PATH.read_text(encoding="utf-8"))
    return {}


def _save_pool(pool: dict):
    POOL_PATH.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")


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


def _job_text(job: dict) -> str:
    return " ".join(str(job.get(k, "") or "") for k in (
        "title", "company", "location", "description", "url"
    )).lower()


def _match_any(text: str, terms: list[str]) -> bool:
    text = text.lower()
    return any(str(term).lower() in text for term in terms)


def _merged_list(search: dict, src_cfg: dict, key: str) -> list[str]:
    return list(search.get(key, [])) + list(src_cfg.get(key, []))


def _apply_post_filters(jobs: list[dict], search: dict, src_cfg: dict, stats: dict) -> list[dict]:
    """Apply local hard filters after a source returns results.

    Job boards treat location/search terms as hints. These filters are the local
    contract: only jobs matching the user's target geography and direction enter
    the pool.
    """
    require_location_terms = _merged_list(search, src_cfg, "require_location_terms")
    reject_location_terms = _merged_list(search, src_cfg, "reject_location_terms")
    require_any_terms = _merged_list(search, src_cfg, "require_any_terms")
    exclude_terms = _merged_list(search, src_cfg, "exclude")

    kept = jobs
    if require_location_terms:
        kept = [j for j in kept if _match_any(_job_text(j), require_location_terms)]
        stats["after_location"] = len(kept)
    if reject_location_terms:
        kept = [j for j in kept if not _match_any(_job_text(j), reject_location_terms)]
        stats["after_reject_location"] = len(kept)
    if require_any_terms:
        kept = [j for j in kept if _match_any(_job_text(j), require_any_terms)]
        stats["after_required_terms"] = len(kept)
    if exclude_terms:
        kept = [j for j in kept if not _match_any(_job_text(j), exclude_terms)]
        stats["after_post_exclude"] = len(kept)
    return kept


def _matches_search_filters(job: dict, search: dict) -> bool:
    stats = {}
    return bool(_apply_post_filters([job], search, {}, stats))


def _cleanup_nonmatching_pending(pool: dict, searches: list[dict]) -> dict:
    to_delete = []
    filtered_searches = [
        s for s in searches
        if any(s.get(k) for k in ("require_location_terms", "reject_location_terms",
                                  "require_any_terms", "exclude"))
    ]
    if not filtered_searches:
        return pool

    for job_id, job in pool.items():
        if job.get("evaluated") or job.get("source") == "manual":
            continue
        job_type = job.get("type") or "job"
        candidate_searches = [s for s in filtered_searches if s.get("type") == job_type]
        if candidate_searches and not any(_matches_search_filters(job, s) for s in candidate_searches):
            to_delete.append(job_id)

    for job_id in to_delete:
        del pool[job_id]
    if to_delete:
        print(f"  cleaned up nonmatching pending entries: {len(to_delete)}")
    return pool


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
    remote_only = bool(load_config().get("remote_only", False))
    searches = load_searches()

    title_seen = {
        (_normalize_title(j.get("title", "")),
         j.get("company", "").lower().strip(),
         canonical_url(j.get("url", "")))
        for j in pool.values()
    }

    for search in searches:
        job_type = search["type"]
        for src_cfg in search["sources"]:
            src_id = src_cfg["id"]
            fn = SOURCE_REGISTRY[src_cfg["source"]]
            inc = src_cfg["include"] if "include" in src_cfg else search.get("include", [])
            exc = src_cfg["exclude"] if "exclude" in src_cfg else search.get("exclude", [])
            kwargs = {"include": inc, "exclude": exc}
            if "max_results" in src_cfg:
                kwargs["max_results"] = src_cfg["max_results"]
            for opt in ("country", "location", "hours_old"):
                if opt in src_cfg:
                    kwargs[opt] = src_cfg[opt]

            try:
                jobs, stats = fn(**kwargs)
            except Exception as e:
                print(f"  [{src_id}] source failed: {e}")
                continue

            # remote-only: drop non-remote postings regardless of source
            if remote_only:
                before = len(jobs)
                jobs = [j for j in jobs if looks_remote(j)]
                stats["after_remote"] = len(jobs)
                if before != len(jobs):
                    print(f"  [{src_id}] remote-only: kept {len(jobs)}/{before}")

            jobs = _apply_post_filters(jobs, search, src_cfg, stats)

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
                    "location":    job.get("location", ""),
                    "url":         job.get("url", ""),
                    "description": job.get("description", ""),
                    "type":        job_type,
                    "source":      src_id,
                    "fetched_at":  today,
                    "evaluated":   False,
                    "is_remote":   job.get("is_remote"),
                }
                seen.add(job["id"])
                title_seen.add(title_key)
                new_count += 1

            search_stats[src_id] = {**stats, "new_to_pool": new_count}
            post_bits = []
            for key in ("after_location", "after_reject_location", "after_required_terms", "after_post_exclude"):
                if key in stats:
                    post_bits.append(f"{key.replace('after_', 'after-')} {stats[key]}")
            post_summary = " " + " ".join(post_bits) if post_bits else ""
            print(f"  [{src_id}] fetched {stats['fetched']} after-include {stats['after_include']} after-exclude {stats['after_exclude']}{post_summary} new-to-pool {new_count}")

    pool = _process_inject_queue(pool, seen)
    pool = _cleanup_nonmatching_pending(pool, searches)
    pool = _cleanup_pool(pool)
    _save_pool(pool)

    LAST_RUN_PATH.write_text(json.dumps(
        {"run_at": today, "searches": search_stats},
        ensure_ascii=False, indent=2
    ), encoding="utf-8")

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
