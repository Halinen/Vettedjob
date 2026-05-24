"""
tests/test_evaluate_jobs.py — Integration tests (mock match_jobs + send_email).
Verify evaluate_all()'s CSV writing, pool marking, and URL-normalized matching.
"""
import csv
import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import evaluate_jobs
from evaluate_jobs import (
    _append_eval_log,
    evaluate_all,
    EVAL_LOG_FIELDS,
)


# ── _append_eval_log ──────────────────────────────────────────────────────────

class TestAppendEvalLog:
    def test_creates_file_with_header_if_absent(self, tmp_path):
        log = tmp_path / "eval_log.csv"
        with patch.object(evaluate_jobs, "EVAL_LOG_PATH", log):
            _append_eval_log([{"id": "j1", "direction": "phd"}])
        with log.open() as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert rows[0]["id"] == "j1"

    def test_appends_without_duplicate_header(self, tmp_path):
        log = tmp_path / "eval_log.csv"
        row = {f: "" for f in EVAL_LOG_FIELDS}
        row["id"] = "j1"
        with patch.object(evaluate_jobs, "EVAL_LOG_PATH", log):
            _append_eval_log([row])
            _append_eval_log([{**row, "id": "j2"}])
        with log.open() as f:
            lines = f.readlines()
        # Only one header line
        header_lines = [l for l in lines if l.startswith("id,")]
        assert len(header_lines) == 1

    def test_extra_fields_ignored(self, tmp_path):
        log = tmp_path / "eval_log.csv"
        row = {f: "" for f in EVAL_LOG_FIELDS}
        row["id"] = "j1"
        row["extra_field"] = "should_be_ignored"
        with patch.object(evaluate_jobs, "EVAL_LOG_PATH", log):
            _append_eval_log([row])  # should not raise
        with log.open() as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames
        assert "extra_field" not in cols


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_pool_job(job_id: str, job_type: str = "phd",
                   manual_score=None, url: str = "") -> dict:
    return {
        "title": f"Job {job_id}", "company": "KTH",
        "url": url or f"https://kth.se/{job_id}",
        "description": "Test description",
        "type": job_type, "source": "varbi",
        "fetched_at": date.today().isoformat(),
        "evaluated": False,
        **({"manual_score": manual_score} if manual_score is not None else {}),
    }


def _make_eval_cfg(job_type: str, tmp_path: Path) -> dict:
    profile = tmp_path / "resume.md"
    profile.write_text("Candidate profile")
    sys_prompt = tmp_path / "system.md"
    sys_prompt.write_text("Evaluate jobs: {profile}\n{shared}")
    shared = tmp_path / "_shared.md"
    shared.write_text("Shared rules")
    return {
        "type": job_type, "model": "claude-sonnet-4-6",
        "max_tokens": 1000, "max_per_call": 40,
        "profile": str(profile),
        "system_prompt": str(sys_prompt),
        "shared": str(shared),
    }


def _run_evaluate_all(tmp_path: Path, pool: dict, eval_cfgs: list,
                      match_return: tuple = ([], [])):
    """
    Run evaluate_all with mocked dependencies.
    match_return: (matched_list, evaluated_list) returned by match_jobs.
    """
    pool_path    = tmp_path / "pool.json"
    eval_log     = tmp_path / "eval_log.csv"
    last_run     = tmp_path / "last_run.json"

    pool_path.write_text(json.dumps(pool, ensure_ascii=False))

    with patch.object(evaluate_jobs, "POOL_PATH",     pool_path), \
         patch.object(evaluate_jobs, "EVAL_LOG_PATH", eval_log), \
         patch.object(evaluate_jobs, "LAST_RUN_PATH", last_run), \
         patch("evaluate_jobs.load_evals", return_value=eval_cfgs), \
         patch("evaluate_jobs.match_jobs", return_value=match_return), \
         patch("evaluate_jobs.send_email"):
        result = evaluate_all()

    return result, pool_path, eval_log


# ── evaluate_all — manual score path ─────────────────────────────────────────

class TestEvaluateAllManualScore:
    def test_manual_scored_job_skips_claude(self, tmp_path):
        pool = {"j1": _make_pool_job("j1", manual_score=8)}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        # match_jobs should NOT be called; if it is, returns empty
        _, _, eval_log = _run_evaluate_all(tmp_path, pool, eval_cfgs,
                                            match_return=([], []))
        with eval_log.open() as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["score"] == "8"
        assert rows[0]["score_source"] == "manual"

    def test_manual_scored_job_marked_matched(self, tmp_path):
        pool = {"j1": _make_pool_job("j1", manual_score=7)}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, _, eval_log = _run_evaluate_all(tmp_path, pool, eval_cfgs)
        with eval_log.open() as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["matched"] == "True"

    def test_manual_scored_pool_entry_marked_evaluated(self, tmp_path):
        pool = {"j1": _make_pool_job("j1", manual_score=9)}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, pool_path, _ = _run_evaluate_all(tmp_path, pool, eval_cfgs)
        updated_pool = json.loads(pool_path.read_text())
        assert updated_pool["j1"]["evaluated"] is True


# ── evaluate_all — Claude score path ─────────────────────────────────────────

class TestEvaluateAllClaudeScore:
    def test_high_score_job_logged_as_matched(self, tmp_path):
        pool = {"j1": _make_pool_job("j1", url="https://kth.se/job/1")}
        matched = [{"url": "https://kth.se/job/1", "score": 8, "reason": "Good",
                    "research_fit": "", "visa_ok": "yes", "visa_note": "",
                    "location": "Stockholm", "contact_email": "",
                    "deadline": "", "application_method": "web",
                    "security_flag": False, "security_note": "",
                    "title": "Job j1", "company": "KTH"}]
        # evaluated list must include id (mirrors evaluate_all's auto_pending construction)
        evaluated = [{"id": "j1", **pool["j1"]}]
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, _, eval_log = _run_evaluate_all(tmp_path, pool, eval_cfgs,
                                            match_return=(matched, evaluated))
        with eval_log.open() as f:
            rows = list(csv.DictReader(f))
        assert len(rows) >= 1

    def test_low_score_job_logged_as_not_matched(self, tmp_path):
        """Score < 6 should result in matched=False in log."""
        url = "https://kth.se/job/low"
        pool = {"jlow": _make_pool_job("jlow", url=url)}
        # match_jobs returns the job with low score in matched list
        # BUT evaluate_all filters to score >= 6, so it won't appear in matched
        low_score_job = {"url": url, "score": 4, "reason": "Poor fit",
                         "research_fit": "", "visa_ok": "yes", "visa_note": "",
                         "location": "", "contact_email": "", "deadline": "",
                         "application_method": "web", "security_flag": False,
                         "security_note": "", "title": "Low", "company": "KTH"}
        job_with_id = {"id": "jlow", **pool["jlow"]}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, _, eval_log = _run_evaluate_all(
            tmp_path, pool, eval_cfgs,
            match_return=([low_score_job], [job_with_id])
        )
        with eval_log.open() as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["matched"] == "False"

    def test_claude_failure_leaves_pool_unevaluated(self, tmp_path):
        """match_jobs returning ([], []) means nothing was evaluated — pool stays False."""
        pool = {"j1": _make_pool_job("j1")}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, pool_path, _ = _run_evaluate_all(tmp_path, pool, eval_cfgs,
                                             match_return=([], []))
        updated = json.loads(pool_path.read_text())
        assert updated["j1"]["evaluated"] is False

    def test_pool_marked_evaluated_after_success(self, tmp_path):
        url = "https://kth.se/job/ok"
        pool = {"jok": _make_pool_job("jok", url=url)}
        matched = [{"url": url, "score": 7, "reason": "Good",
                    "research_fit": "", "visa_ok": "yes", "visa_note": "",
                    "location": "", "contact_email": "", "deadline": "",
                    "application_method": "web", "security_flag": False,
                    "security_note": "", "title": "Job", "company": "KTH"}]
        job_with_id = {"id": "jok", **pool["jok"]}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, pool_path, _ = _run_evaluate_all(
            tmp_path, pool, eval_cfgs,
            match_return=(matched, [job_with_id])
        )
        updated = json.loads(pool_path.read_text())
        assert updated["jok"]["evaluated"] is True


# ── Bug 2 regression: URL normalization matching ──────────────────────────────

class TestUrlNormalizationRegression:
    """
    Regression test: when the URL returned by Claude has a trailing slash, it should still correctly match the URL in the pool.
    Before the fix: a direct == comparison caused is_matched=False, even though evaluated=True had already been marked.
    After the fix: compare after canonical_url normalization.
    """

    def test_trailing_slash_url_still_matches(self, tmp_path):
        url_in_pool  = "https://kth.se/job/42"
        url_from_claude = "https://kth.se/job/42/"   # Claude returned an extra trailing slash

        pool = {"j42": _make_pool_job("j42", url=url_in_pool)}
        matched = [{"url": url_from_claude, "score": 8, "reason": "Fit",
                    "research_fit": "", "visa_ok": "yes", "visa_note": "",
                    "location": "", "contact_email": "", "deadline": "",
                    "application_method": "web", "security_flag": False,
                    "security_note": "", "title": "Job", "company": "KTH"}]
        job_with_id = {"id": "j42", **pool["j42"]}
        eval_cfgs = [_make_eval_cfg("phd", tmp_path)]
        _, _, eval_log = _run_evaluate_all(
            tmp_path, pool, eval_cfgs,
            match_return=(matched, [job_with_id])
        )
        with eval_log.open() as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["matched"] == "True", (
            "a trailing-slash difference in the URL should not cause a match failure (Bug 2 regression)"
        )
        assert rows[0]["score"] == "8"
