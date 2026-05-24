"""
tests/test_sync_index.py — Tests for the two modes of sync_index.py and its helper functions.
Everything uses temporary directories; no dependency on real data files.
"""
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import sync_index
from sync_index import _waiting_days, sync, INDEX_FIELDS, EVAL_COLS, USER_COLS


# ── _waiting_days ─────────────────────────────────────────────────────────────

class TestWaitingDays:
    def test_empty_string_returns_empty(self):
        assert _waiting_days("") == ""

    def test_valid_date_returns_integer_string(self):
        applied = (date.today() - timedelta(days=5)).isoformat()
        result = _waiting_days(applied)
        assert result == "5"

    def test_today_returns_zero(self):
        assert _waiting_days(date.today().isoformat()) == "0"

    def test_invalid_date_returns_empty(self):
        assert _waiting_days("not-a-date") == ""

    def test_malformed_iso_returns_empty(self):
        assert _waiting_days("2026-13-01") == ""


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_eval_row(**kwargs) -> dict:
    defaults = {
        "id": "job_001", "evaluated_at": "2026-04-01", "direction": "phd",
        "source": "varbi", "title": "PhD in Physics", "company": "KTH",
        "location": "Stockholm", "url": "https://kth.se/job/1",
        "contact_email": "", "score": "8", "score_source": "claude",
        "matched": "True", "visa_ok": "yes", "visa_note": "",
        "reason": "Good fit", "research_fit": "Plasma", "deadline": "",
        "application_method": "web", "security_flag": "False", "security_note": "",
    }
    return {**defaults, **kwargs}


def _write_eval_log(path: Path, rows: list[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EVAL_COLS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_index(path: Path, rows: list[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=INDEX_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _read_index(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── sync(mode="append") ───────────────────────────────────────────────────────

class TestSyncAppend:
    def test_appends_new_rows(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"

        _write_eval_log(eval_log, [_make_eval_row(id="j1"), _make_eval_row(id="j2")])
        # Start with j1 already in index
        _write_index(index, [{**_make_eval_row(id="j1"), **{c: "" for c in USER_COLS},
                               "workspace_created": False, "has_cv": False, "has_cl": False}])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index):
            sync(mode="append")

        rows = _read_index(index)
        ids = [r["id"] for r in rows]
        assert "j1" in ids
        assert "j2" in ids
        assert len(ids) == 2

    def test_does_not_duplicate_existing_rows(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"

        _write_eval_log(eval_log, [_make_eval_row(id="j1")])
        _write_index(index, [{**_make_eval_row(id="j1"), **{c: "" for c in USER_COLS},
                               "workspace_created": False, "has_cv": False, "has_cl": False}])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index):
            sync(mode="append")

        rows = _read_index(index)
        assert len(rows) == 1

    def test_new_rows_default_status_pending(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"
        _write_eval_log(eval_log, [_make_eval_row(id="j1")])
        _write_index(index, [])   # empty but has header

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index):
            sync(mode="append")

        rows = _read_index(index)
        assert rows[0]["status"] == "pending"

    def test_idempotent_multiple_runs(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"
        _write_eval_log(eval_log, [_make_eval_row(id="j1")])
        _write_index(index, [])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index):
            sync(mode="append")
            sync(mode="append")
            sync(mode="append")

        rows = _read_index(index)
        assert len(rows) == 1


# ── sync(mode="full") ─────────────────────────────────────────────────────────

class TestSyncFull:
    def test_rebuild_creates_all_rows(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"

        _write_eval_log(eval_log, [_make_eval_row(id="j1"), _make_eval_row(id="j2")])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index), \
             patch.object(sync_index, "JOBS_DIR", tmp_path / "jobs"):
            sync(mode="full")

        rows = _read_index(index)
        assert len(rows) == 2

    def test_rebuild_preserves_user_decision(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"

        _write_eval_log(eval_log, [_make_eval_row(id="j1")])
        # Simulate user having set review_decision = "accepted"
        existing_row = {**_make_eval_row(id="j1"), **{c: "" for c in USER_COLS},
                        "workspace_created": False, "has_cv": False, "has_cl": False}
        existing_row["review_decision"] = "accepted"
        existing_row["status"] = "applying"
        _write_index(index, [existing_row])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index), \
             patch.object(sync_index, "JOBS_DIR", tmp_path / "jobs"):
            sync(mode="full")

        rows = _read_index(index)
        assert rows[0]["review_decision"] == "accepted"
        assert rows[0]["status"] == "applying"

    def test_rebuild_new_job_gets_pending_status(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"

        _write_eval_log(eval_log, [_make_eval_row(id="j_new")])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index), \
             patch.object(sync_index, "JOBS_DIR", tmp_path / "jobs"):
            sync(mode="full")

        rows = _read_index(index)
        assert rows[0]["status"] == "pending"

    def test_rebuild_detects_workspace(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"
        jobs_dir = tmp_path / "jobs"

        _write_eval_log(eval_log, [_make_eval_row(id="j1")])

        # Create a fake workspace with status.json
        ws = jobs_dir / "2026-04-01_kth_phd"
        ws.mkdir(parents=True)
        (ws / "status.json").write_text(json.dumps({"job_id": "j1"}))
        (ws / "cv").mkdir()
        (ws / "cl").mkdir()

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index), \
             patch.object(sync_index, "JOBS_DIR", jobs_dir):
            sync(mode="full")

        rows = _read_index(index)
        assert rows[0]["workspace_created"] == "True"
        assert rows[0]["has_cv"] == "False"    # no cv.pdf
        assert rows[0]["has_cl"] == "False"

    def test_rebuild_detects_compiled_pdf(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"
        jobs_dir = tmp_path / "jobs"

        _write_eval_log(eval_log, [_make_eval_row(id="j1")])

        ws = jobs_dir / "2026-04-01_kth_phd"
        ws.mkdir(parents=True)
        (ws / "status.json").write_text(json.dumps({"job_id": "j1"}))
        (ws / "cv").mkdir()
        (ws / "cl").mkdir()
        (ws / "cv" / "cv.pdf").touch()
        (ws / "cl" / "cl.pdf").touch()

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index), \
             patch.object(sync_index, "JOBS_DIR", jobs_dir):
            sync(mode="full")

        rows = _read_index(index)
        assert rows[0]["has_cv"] == "True"
        assert rows[0]["has_cl"] == "True"

    def test_empty_eval_log_produces_empty_index(self, tmp_path):
        eval_log = tmp_path / "eval_log.csv"
        index    = tmp_path / "jobs_index.csv"
        _write_eval_log(eval_log, [])

        with patch.object(sync_index, "EVAL_LOG", eval_log), \
             patch.object(sync_index, "INDEX", index), \
             patch.object(sync_index, "JOBS_DIR", tmp_path / "jobs"):
            sync(mode="full")

        rows = _read_index(index)
        assert rows == []
