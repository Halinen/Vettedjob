"""
sync_index.py — build jobs_index.csv.

Two modes:
  append (default): only append new entries from eval_log; existing rows untouched
  full / --rebuild: rebuild entirely from eval_log, preserving user-maintained columns

Usage:
  python3 scripts/sync_index.py            # append mode (errors if jobs_index.csv missing)
  python3 scripts/sync_index.py --rebuild  # full rebuild
"""

import csv
import sys
from datetime import date
from pathlib import Path

EVAL_LOG  = Path("data/eval_log.csv")
INDEX     = Path("jobs_index.csv")
JOBS_DIR  = Path("jobs")

# eval-derived columns (read from eval_log on every append/rebuild)
EVAL_COLS = [
    "id", "evaluated_at", "direction", "source",
    "title", "company", "location", "url", "contact_email",
    "score", "score_source", "matched", "visa_ok", "visa_note",
    "reason", "research_fit", "deadline", "application_method",
    "security_flag", "security_note",
    "legit_verdict", "legit_score", "legit_red_flags", "legit_green_flags",
]

# user-maintained columns (kept from the old jobs_index on rebuild; empty/default on append)
USER_COLS = [
    "review_decision",  # "accepted" | "rejected" | ""
    "review_reason",
    "status",           # pending | ignore | ready | applying | applied | interview | rejected | offer
    "applied_date", "waiting_days", "result", "notes",
]

# workspace-status columns (populated by scanning jobs/ on rebuild)
WS_COLS = [
    "workspace_created", "has_cv", "has_cl",
]

INDEX_FIELDS = EVAL_COLS + USER_COLS + WS_COLS


def _read_eval_log() -> list[dict]:
    if not EVAL_LOG.exists():
        return []
    with EVAL_LOG.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _read_index() -> dict[str, dict]:
    """Return {id: row}, preserving content the user has edited."""
    if not INDEX.exists():
        return {}
    with INDEX.open(encoding="utf-8") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def _find_workspace(job_id: str) -> Path | None:
    """Find the local workspace folder for a job_id."""
    if not JOBS_DIR.exists():
        return None
    import json
    for folder in JOBS_DIR.iterdir():
        sf = folder / "status.json"
        if sf.exists():
            try:
                s = json.loads(sf.read_text())
                if s.get("job_id") == job_id:
                    return folder
            except Exception:
                pass
    return None


def _waiting_days(applied_date: str) -> str:
    if not applied_date:
        return ""
    try:
        return str((date.today() - date.fromisoformat(applied_date)).days)
    except ValueError:
        return ""


def sync(mode: str = "append"):
    """
    mode="append"  cloud/default: only add new rows, leave existing rows alone
    mode="full"    local rebuild: rebuild entirely, reading local status.json
    """
    eval_rows = _read_eval_log()
    existing  = _read_index()   # {id: row}

    if mode == "append":
        new_ids = [r["id"] for r in eval_rows if r["id"] not in existing]
        if not new_ids:
            print("  jobs_index.csv: no new entries")
            return

        write_header = not INDEX.exists() or INDEX.stat().st_size == 0
        with INDEX.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            for row in eval_rows:
                if row["id"] in new_ids:
                    writer.writerow({
                        **row,
                        "review_decision": "",
                        "review_reason":   "",
                        "status":          "pending",
                        "applied_date":    "",
                        "waiting_days":    "",
                        "result":          "",
                        "notes":           "",
                        "workspace_created": False,
                        "has_cv":          False,
                        "has_cl":          False,
                    })
        print(f"  jobs_index.csv: appended {len(new_ids)} entries")

    else:  # full / rebuild
        rows = []
        for row in eval_rows:
            job_id  = row["id"]
            out_row = {f: row.get(f, "") for f in INDEX_FIELDS}

            # prefer the user-edited index row (preserve user-maintained columns)
            if job_id in existing:
                ex = existing[job_id]
                for col in USER_COLS:
                    if ex.get(col) is not None:
                        out_row[col] = ex[col]
            else:
                out_row["status"] = "pending"

            # update waiting days
            out_row["waiting_days"] = _waiting_days(out_row.get("applied_date", ""))

            # scan the local workspace
            ws = _find_workspace(job_id)
            if ws:
                out_row["workspace_created"] = True
                out_row["has_cv"] = (ws / "cv" / "cv.pdf").exists()
                out_row["has_cl"] = (ws / "cl" / "cl.pdf").exists()
            else:
                out_row["workspace_created"] = False
                out_row["has_cv"] = False
                out_row["has_cl"] = False

            rows.append(out_row)

        with INDEX.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"  jobs_index.csv: full rebuild, {len(rows)} entries")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args()

    import os
    os.chdir(Path(__file__).parent.parent)

    if args.rebuild:
        sync(mode="full")
    else:
        if not INDEX.exists():
            print("error: jobs_index.csv not found.")
            print("On first use or if the file is missing, run: python3 scripts/sync_index.py --rebuild")
            sys.exit(1)
        sync(mode="append")
