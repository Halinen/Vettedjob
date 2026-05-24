"""
diagnose_eval.py — Re-invoke Claude for jobs already evaluated on a given date and print the raw response.
Read-only; does not modify pool.json / eval_log.csv.

Usage:
  python3 scripts/diagnose_eval.py             # Default today, take the first 20 of each job+phd
  python3 scripts/diagnose_eval.py --date 2026-04-20 --type job --limit 10
"""

import argparse
import csv
import json
import sys
import os
from datetime import date
from pathlib import Path

os.chdir(Path(__file__).parent.parent)
sys.path.insert(0, str(Path(__file__).parent))

from utils import get_client

EVAL_LOG_PATH = Path("data/eval_log.csv")
POOL_PATH     = Path("data/pool.json")


def load_jobs_for_date(target_date: str, job_type: str | None) -> list[dict]:
    """Get the IDs evaluated on the given date from eval_log, then fetch full info from pool."""
    with EVAL_LOG_PATH.open(encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f)
                if r["evaluated_at"] == target_date
                and (job_type is None or r["direction"] == job_type)]

    pool = json.loads(POOL_PATH.read_text(encoding="utf-8"))
    jobs = []
    for row in rows:
        jid = row["id"]
        if jid in pool:
            jobs.append({"id": jid, **pool[jid]})
        else:
            # Expired in pool (30-day TTL); fall back to the basic fields from eval_log
            jobs.append({
                "id":          jid,
                "title":       row["title"],
                "company":     row["company"],
                "url":         row["url"],
                "description": "(description not available — pool entry expired)",
            })
    return jobs


def run_diagnosis(jobs: list[dict], eval_cfg: dict):
    profile          = Path(eval_cfg["profile"]).read_text(encoding="utf-8")
    system_template  = Path(eval_cfg["system_prompt"]).read_text(encoding="utf-8")
    shared_path      = Path(eval_cfg.get("shared", "data/prompts/_shared.md"))
    shared_content   = shared_path.read_text(encoding="utf-8") if shared_path.exists() else ""
    system_prompt    = (system_template
                        .replace("{profile}", profile)
                        .replace("{shared}", shared_content))

    # Diagnostic mode: override output filtering instructions to force returning scores for all jobs
    import re
    system_prompt = re.sub(
        r"Return only postings with score >= \d+.+",
        "Return scoring results for all jobs (regardless of score, no filtering), sorted by score in descending order.",
        system_prompt
    )

    jobs_text = "\n\n".join(
        f"[{i+1}] {j['title']} @ {j.get('company', '')}\nURL: {j.get('url', '')}\n{j.get('description', '')}"
        for i, j in enumerate(jobs)
    )

    model      = eval_cfg.get("model", "claude-sonnet-4-6")
    max_tokens = eval_cfg.get("max_tokens", 4000)

    print(f"\n{'='*60}")
    print(f"Sending {len(jobs)} jobs to Claude ({model})")
    print(f"{'='*60}\n")

    response = get_client().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": jobs_text}]
    )
    raw = response.content[0].text.strip()

    print("[Claude raw response]")
    print(raw)
    print()

    # Try to parse and tally the score distribution
    start = raw.find("[")
    end   = raw.rfind("]") + 1
    if start == -1 or end == 0:
        print("⚠️  No JSON array found; Claude may have returned plain text or an error")
        return

    try:
        results = json.loads(raw[start:end])
    except json.JSONDecodeError as e:
        print(f"⚠️  JSON parsing failed: {e}")
        return

    if not results:
        print("ℹ️  Claude returned an empty array [] — all jobs scored below the threshold (score < 6 or visa_ok=false)")
    else:
        print(f"\n[Match summary] {len(results)} entries with score >= 6:")
        for r in sorted(results, key=lambda x: -float(x.get("score", 0))):
            print(f"  [{r.get('score')}] {r.get('title')} @ {r.get('company')} — {r.get('reason', '')[:60]}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date",  default=date.today().isoformat())
    parser.add_argument("--type",  choices=["job", "phd"], default=None)
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    evals = {
        cfg["type"]: cfg
        for cfg in [
            json.loads(Path(f).read_text(encoding="utf-8"))
            for f in sorted(Path("evals").glob("*.json"))
        ]
    }

    types = [args.type] if args.type else ["job", "phd"]
    for t in types:
        jobs = load_jobs_for_date(args.date, t)
        if not jobs:
            print(f"\n[{t}] No evaluation records for this date, skipping")
            continue
        sample = jobs[:args.limit]
        print(f"\n[{t}] {len(jobs)} entries total; diagnosing the first {len(sample)} this time")
        run_diagnosis(sample, evals[t])


if __name__ == "__main__":
    main()
