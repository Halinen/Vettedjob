"""
workspace.py — local per-job workspace creation.
Triggered from review_gui.py (review_decision=accepted).

CLI:
  python3 scripts/workspace.py --job-id <id>
"""

import csv
import json
import re
import sys
import os
from datetime import date
from pathlib import Path

os.chdir(Path(__file__).parent.parent)

JOBS_DIR  = Path("jobs")
INDEX     = Path("jobs_index.csv")

def _make_folder_name(company: str, title: str) -> str:
    today   = date.today().isoformat()
    company = re.sub(r"[^\w]", "-", company)[:20]
    title   = re.sub(r"[^\w]", "-", title)[:30]
    return f"{today}_{company}_{title}".lower().replace("--", "-")


def create_workspace(job: dict) -> Path | None:
    """Create a workspace folder for one job. `job` is a jobs_index.csv row (dict)."""
    folder_name = _make_folder_name(
        job.get("company", "unknown"),
        job.get("title", "job"),
    )
    folder = JOBS_DIR / folder_name
    if folder.exists():
        print(f"  workspace already exists: {folder.name}")
        return folder

    JOBS_DIR.mkdir(exist_ok=True)
    folder.mkdir(parents=True)
    (folder / "cl").mkdir()
    (folder / "cv").mkdir()

    # status.json：job_id + timeline
    status = {
        "job_id":   job.get("id", ""),
        "timeline": [
            {"date":  date.today().isoformat(),
             "event": "workspace_created",
             "note":  ""}
        ],
        "notes": "",
    }
    (folder / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")

    # job_info.md
    score = job.get("score", "?")
    legit = job.get("legit_verdict", "")
    lscore = job.get("legit_score", "")
    (folder / "job_info.md").write_text(
        f"# {job.get('title', '')} @ {job.get('company', '')}\n\n"
        f"**Type**: {job.get('direction', '')}  \n"
        f"**Fit score**: {score}/10  \n"
        f"**Legitimacy**: {legit.upper()} {lscore}/10  \n"
        f"**Deadline**: {job.get('deadline', 'unknown')}  \n"
        f"**URL**: {job.get('url', '')}  \n"
        f"**Apply via**: {job.get('application_method', '')}  \n"
        f"**Contact**: {job.get('contact_email', '')}  \n\n"
        f"## Fit analysis\n{job.get('reason', '')}\n\n"
        f"## Legitimacy flags\n"
        f"- Green: {job.get('legit_green_flags', '')}\n"
        f"- Red: {job.get('legit_red_flags', '')}\n",
        encoding="utf-8",
    )

    # forms.md
    (folder / "forms.md").write_text(
        f"# {job.get('title', '')} — application form notes\n\n"
        "## Personal info\n"
        "- Name:\n"
        "- Email:\n"
        "- Phone:\n"
        "- Address:\n\n"
        "## Motivation statement (100-200 words, paste into the form)\n"
        "> TODO — distill from the cover letter in cl/\n\n"
        "## Other form fields\n"
        "> Record any job-specific form requirements here\n",
        encoding="utf-8",
    )

    # notes.md
    (folder / "notes.md").write_text(
        f"# Notes — {job.get('title', '')}\n\n"
        "> Your thoughts, interview prep, follow-up plans, etc.\n",
        encoding="utf-8",
    )

    # TASK.md — Claude Code task brief
    folder_rel = folder.resolve().relative_to(Path.cwd())

    (folder / "TASK.md").write_text(
        f"# Task: write CL / CV for {job.get('title', '')} @ {job.get('company', '')}\n\n"
        "## What to do\n\n"
        "1. Read the input files below to understand the role and the candidate.\n"
        "2. Using the CL template, write a tailored cover letter into `cl/cl.tex`.\n"
        "3. Copy the CV template to `cv/cv.tex` and adjust it for this role.\n"
        "4. When done, summarise what you changed and why.\n\n"
        "## Input files\n\n"
        f"| File | Purpose |\n"
        f"| --- | --- |\n"
        f"| [`job_info.md`]({folder_rel}/job_info.md) | Job info, fit analysis, legitimacy verdict |\n"
        f"| [`data/profiles/resume_eval.md`](data/profiles/resume_eval.md) | Candidate profile |\n"
        "| `data/base_templates/cv_<type>.tex` | CV base template (pick from the list below) |\n"
        "| `data/base_templates/cl_<type>.tex` | CL base template (pick from the list below) |\n\n"
        "## Template list\n\n"
        "Pick the most suitable template pair for this role from "
        "`data/base_templates/`. CV and CL may be mixed across types.\n\n"
        "## Output files\n\n"
        f"| File | Description |\n"
        f"| --- | --- |\n"
        f"| [`cl/cl.tex`]({folder_rel}/cl/cl.tex) | Tailored cover letter (LaTeX) |\n"
        f"| [`cv/cv.tex`]({folder_rel}/cv/cv.tex) | Adjusted CV (LaTeX) |\n\n"
        "## Cover-letter requirements\n\n"
        "- The opening paragraph should clearly connect to the role's focus.\n"
        "- The body should highlight the 1-2 most relevant experiences.\n"
        "- Keep the closing concise.\n"
        "- Preserve the LaTeX format and the template's overall style.\n"
        "- Language: English.\n\n"
        "## Quick job summary\n\n"
        f"- **Title**: {job.get('title', '')}\n"
        f"- **Company**: {job.get('company', '')}\n"
        f"- **Fit score**: {score}/10\n"
        f"- **Legitimacy**: {legit.upper()} {lscore}/10\n"
        f"- **Deadline**: {job.get('deadline', 'unknown')}\n"
        f"- **Fit reason**: {job.get('reason', '')}\n",
        encoding="utf-8",
    )

    print(f"  workspace created: {folder.name}")
    print(f"  -> In Claude Code: read {folder_rel}/TASK.md to generate CL/CV")
    return folder


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()

    if not INDEX.exists():
        print("error: jobs_index.csv not found")
        sys.exit(1)

    with INDEX.open(encoding="utf-8") as f:
        rows = {r["id"]: r for r in csv.DictReader(f)}

    if args.job_id not in rows:
        print(f"error: {args.job_id} not found in jobs_index.csv")
        sys.exit(1)

    create_workspace(rows[args.job_id])
