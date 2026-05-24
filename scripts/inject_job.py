"""
Job injection script — writes manual jobs into inject_queue.json, to be processed on the next fetch_all().
Usage:
  python scripts/inject_job.py                    # interactive entry
  python scripts/inject_job.py --file job.json    # batch inject from a JSON file
  python scripts/inject_job.py --show             # view pending queue (processed=false)
  python scripts/inject_job.py --clear            # delete processed=false entries (processed ones are kept)
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, str(Path(__file__).parent))

from utils import canonical_url

QUEUE_PATH = Path("data/inject_queue.json")
POOL_PATH  = Path("data/pool.json")
TYPES = ["phd", "job"]


def make_id(url: str) -> str:
    digest = hashlib.md5(canonical_url(url).encode()).hexdigest()[:12]
    return f"manual_{digest}"


def _load_queue() -> list:
    if QUEUE_PATH.exists():
        return json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    return []


def _save_queue(queue: list):
    QUEUE_PATH.parent.mkdir(exist_ok=True)
    QUEUE_PATH.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")


def _url_exists(url: str, queue: list) -> bool:
    """Check whether the URL already exists in the queue or pool."""
    canon = canonical_url(url)
    for item in queue:
        if canonical_url(item.get("url", "")) == canon:
            return True
    if POOL_PATH.exists():
        pool = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        for job in pool.values():
            if canonical_url(job.get("url", "")) == canon:
                return True
    return False


def inject_one(job: dict):
    """Write a single job into inject_queue.json"""
    queue = _load_queue()

    url = job.get("url", "")
    if _url_exists(url, queue):
        print(f"⚠️  Same URL already exists, skipping: {job.get('title')}")
        return

    job_id = make_id(url)

    entry = {
        "id":                 job_id,
        "type":               job.get("type", ""),
        "title":              job.get("title", ""),
        "company":            job.get("company", ""),
        "location":           job.get("location", ""),
        "url":                url,
        "contact_email":      job.get("contact_email", ""),
        "application_method": job.get("application_method", "web"),
        "deadline":           job.get("deadline", ""),
        "description":        job.get("description", ""),
        "manual_score":       job.get("manual_score", None),
        "processed":          False,
    }

    queue.append(entry)
    _save_queue(queue)

    score_str = str(entry["manual_score"]) if entry["manual_score"] is not None else "scored by Claude"
    print(f"✓ Added to queue: {entry['title']} @ {entry['company']} | Score: {score_str}")
    pending = sum(1 for i in queue if not i.get("processed"))
    print(f"  {pending} pending in queue")


def interactive_inject():
    """Interactively enter a single job"""
    print("\n=== Manually Inject Job ===\n")

    print("Select type:")
    for i, t in enumerate(TYPES, 1):
        print(f"  {i}. {t}")
    type_idx = int(input("Enter number: ")) - 1
    job_type = TYPES[type_idx]

    job = {
        "type":               job_type,
        "title":              input("Job title: ").strip(),
        "company":            input("Organization/company name: ").strip(),
        "location":           input("City (optional): ").strip(),
        "url":                input("Job link: ").strip(),
        "contact_email":      input("Contact email (optional): ").strip(),
        "application_method": input("Application method (email/web, default web): ").strip() or "web",
        "deadline":           input("Deadline (YYYY-MM-DD, optional): ").strip(),
        "description":        input("Job description (pasting the original is recommended, optional): ").strip(),
    }

    score_input = input("Manual score (1-10, leave blank to let Claude score): ").strip()
    job["manual_score"] = int(score_input) if score_input else None

    inject_one(job)


def inject_from_file(filepath: str):
    """Batch inject from a JSON file"""
    path = Path(filepath)
    if not path.exists():
        print(f"File does not exist: {filepath}")
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data if isinstance(data, list) else [data]
    for job in jobs:
        inject_one(job)


def show_queue():
    queue = _load_queue()
    pending = [i for i in queue if not i.get("processed")]
    if not pending:
        print("No pending injected jobs at the moment")
        return
    print(f"\nPending jobs ({len(pending)}):")
    for idx, item in enumerate(pending, 1):
        score_str = str(item.get("manual_score")) if item.get("manual_score") is not None else "scored by Claude"
        print(f"  {idx}. [{item.get('type','')}] {item.get('title','')} "
              f"@ {item.get('company','')} | Score: {score_str}")


def clear_queue():
    queue = _load_queue()
    before = len([i for i in queue if not i.get("processed")])
    queue = [i for i in queue if i.get("processed")]
    _save_queue(queue)
    print(f"Cleared {before} unprocessed injected jobs (processed entries kept)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file",  help="batch inject from a JSON file")
    parser.add_argument("--show",  action="store_true", help="view pending queue")
    parser.add_argument("--clear", action="store_true", help="clear unprocessed injected jobs")
    args = parser.parse_args()

    if args.show:
        show_queue()
    elif args.clear:
        clear_queue()
    elif args.file:
        inject_from_file(args.file)
    else:
        interactive_inject()


if __name__ == "__main__":
    main()
