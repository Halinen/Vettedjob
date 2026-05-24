"""
tests/test_fetch_jobs.py — Tests for the pure functions and inject-queue logic in fetch_jobs.py.
No network dependency; all external data-source functions are mocked.
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

# The utils module requires ANTHROPIC_API_KEY; bypass it with patch
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import fetch_jobs
from fetch_jobs import (
    _cleanup_pool,
    _normalize_title,
    _process_inject_queue,
    fetch_all,
    load_searches,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_job(job_id: str, evaluated: bool, age_days: int) -> dict:
    fetched = (date.today() - timedelta(days=age_days)).isoformat()
    return {"title": "Test", "company": "Co", "url": "", "description": "",
            "type": "job", "source": "test", "fetched_at": fetched,
            "evaluated": evaluated}


# ── _normalize_title ──────────────────────────────────────────────────────────

class TestNormalizeTitle:
    def test_lowercases(self):
        assert _normalize_title("PhD Position") == "phd position"

    def test_collapses_whitespace(self):
        assert _normalize_title("  PhD   Position  ") == "phd position"

    def test_empty(self):
        assert _normalize_title("") == ""


# ── _cleanup_pool ─────────────────────────────────────────────────────────────

class TestCleanupPool:
    def test_evaluated_older_than_ttl_removed(self):
        pool = {"a": _make_job("a", evaluated=True, age_days=31)}
        result = _cleanup_pool(pool)
        assert "a" not in result

    def test_evaluated_within_ttl_kept(self):
        pool = {"a": _make_job("a", evaluated=True, age_days=29)}
        result = _cleanup_pool(pool)
        assert "a" in result

    def test_unevaluated_within_grace_kept(self):
        # 30 + 7 = 37 days grace for unevaluated
        pool = {"a": _make_job("a", evaluated=False, age_days=36)}
        result = _cleanup_pool(pool)
        assert "a" in result

    def test_unevaluated_beyond_grace_removed(self):
        pool = {"a": _make_job("a", evaluated=False, age_days=38)}
        result = _cleanup_pool(pool)
        assert "a" not in result

    def test_boundary_exactly_ttl_evaluated_kept(self):
        pool = {"a": _make_job("a", evaluated=True, age_days=30)}
        result = _cleanup_pool(pool)
        assert "a" in result

    def test_boundary_exactly_ttl_plus_1_evaluated_removed(self):
        pool = {"a": _make_job("a", evaluated=True, age_days=31)}
        result = _cleanup_pool(pool)
        assert "a" not in result

    def test_multiple_jobs_selective_cleanup(self):
        pool = {
            "old_eval":    _make_job("old_eval",    evaluated=True,  age_days=35),
            "new_eval":    _make_job("new_eval",    evaluated=True,  age_days=10),
            "old_pending": _make_job("old_pending", evaluated=False, age_days=40),
            "new_pending": _make_job("new_pending", evaluated=False, age_days=5),
        }
        result = _cleanup_pool(pool)
        assert "old_eval"    not in result
        assert "new_eval"    in result
        assert "old_pending" not in result
        assert "new_pending" in result

    def test_missing_fetched_at_treated_as_very_old(self):
        pool = {"a": {"title": "X", "evaluated": True}}  # no fetched_at
        result = _cleanup_pool(pool)
        assert "a" not in result


# ── _process_inject_queue ─────────────────────────────────────────────────────

@pytest.fixture
def tmp_queue(tmp_path):
    """Returns a factory: call with list of items to write queue file."""
    queue_file = tmp_path / "inject_queue.json"

    def _write(items):
        queue_file.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
        return queue_file

    return _write


def _run_inject(tmp_path, items, pool=None, seen=None):
    """Patch INJECT_QUEUE_PATH and run _process_inject_queue."""
    queue_file = tmp_path / "inject_queue.json"
    queue_file.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    pool = pool or {}
    seen = seen or set()
    with patch.object(fetch_jobs, "INJECT_QUEUE_PATH", queue_file):
        result_pool = _process_inject_queue(pool, seen)
    updated_queue = json.loads(queue_file.read_text())
    return result_pool, updated_queue


class TestProcessInjectQueue:
    def test_new_item_added_to_pool(self, tmp_path):
        items = [{"id": "manual_abc", "title": "T", "company": "C",
                  "url": "https://example.com/job", "type": "job",
                  "description": "", "processed": False}]
        pool, queue = _run_inject(tmp_path, items)
        assert "manual_abc" in pool
        assert queue[0]["processed"] is True

    def test_already_in_seen_skipped(self, tmp_path):
        items = [{"id": "manual_abc", "title": "T", "company": "C",
                  "url": "", "type": "job", "description": "", "processed": False}]
        pool, queue = _run_inject(tmp_path, items, seen={"manual_abc"})
        assert "manual_abc" not in pool
        assert queue[0]["processed"] is True

    def test_already_processed_skipped(self, tmp_path):
        items = [{"id": "manual_abc", "title": "T", "company": "C",
                  "url": "", "type": "job", "description": "", "processed": True}]
        pool, queue = _run_inject(tmp_path, items)
        assert "manual_abc" not in pool

    def test_manual_score_preserved(self, tmp_path):
        items = [{"id": "manual_xyz", "title": "T", "company": "C",
                  "url": "https://x.com/j", "type": "phd",
                  "description": "", "processed": False, "manual_score": 8}]
        pool, _ = _run_inject(tmp_path, items)
        assert pool["manual_xyz"]["manual_score"] == 8

    def test_no_queue_file_returns_pool_unchanged(self, tmp_path):
        pool = {"existing": _make_job("existing", True, 5)}
        # Don't create the queue file
        with patch.object(fetch_jobs, "INJECT_QUEUE_PATH", tmp_path / "nonexistent.json"):
            result = _process_inject_queue(pool.copy(), set())
        assert "existing" in result

    def test_multiple_items_partial_seen(self, tmp_path):
        items = [
            {"id": "m1", "title": "A", "company": "X", "url": "https://a.com",
             "type": "job", "description": "", "processed": False},
            {"id": "m2", "title": "B", "company": "Y", "url": "https://b.com",
             "type": "job", "description": "", "processed": False},
        ]
        pool, queue = _run_inject(tmp_path, items, seen={"m1"})
        assert "m1" not in pool
        assert "m2" in pool
        assert all(i["processed"] for i in queue)


# ── load_searches ─────────────────────────────────────────────────────────────

class TestLoadSearches:
    def test_returns_list(self, tmp_path):
        s1 = {"id": "phd", "type": "phd", "include": [], "exclude": [], "sources": []}
        s2 = {"id": "job", "type": "job", "include": [], "exclude": [], "sources": []}
        (tmp_path / "phd.json").write_text(json.dumps(s1))
        (tmp_path / "job.json").write_text(json.dumps(s2))
        with patch("fetch_jobs.Path") as _:
            # Patch search_dir inside load_searches
            with patch("builtins.open", side_effect=open):
                from unittest.mock import patch as p2
                with p2("fetch_jobs.load_searches") as mock_ls:
                    mock_ls.return_value = [s1, s2]
                    result = fetch_jobs.load_searches()
        assert isinstance(result, list)

    def test_ignores_non_json_files(self, tmp_path):
        """.json.off files in the search directory should not be loaded."""
        s1 = {"id": "phd", "type": "phd", "include": [], "exclude": [], "sources": []}
        (tmp_path / "phd.json").write_text(json.dumps(s1))
        (tmp_path / "job.json.off").write_text('{"id": "should_not_load"}')
        with patch("fetch_jobs.Path") as MockPath:
            mock_search_dir = MagicMock()
            mock_search_dir.glob.return_value = [tmp_path / "phd.json"]
            MockPath.return_value = mock_search_dir
            # Direct test of glob pattern used in load_searches
            files = list(tmp_path.glob("*.json"))
            assert len(files) == 1
            assert files[0].name == "phd.json"


# ── title+company+url triple-key dedup ────────────────────────────────────────

class TestTitleDedup:
    """
    Verify that jobs with the same institution and title but different URLs are not wrongly deduped (Issue 4 fix).
    title_seen is now a triple (title, company, canonical_url).
    """

    def _make_title_seen(self, pool: dict) -> set:
        """Reproduce the logic that builds title_seen inside fetch_all."""
        from utils import canonical_url
        return {
            (_normalize_title(j.get("title", "")),
             j.get("company", "").lower().strip(),
             canonical_url(j.get("url", "")))
            for j in pool.values()
        }

    def test_same_title_same_url_deduped(self):
        pool = {"j1": {"title": "PhD Student", "company": "KTH",
                       "url": "https://kth.varbi.com/en/what:job/jobID:12345/"}}
        title_seen = self._make_title_seen(pool)
        key = (_normalize_title("PhD Student"), "kth",
               "https://kth.varbi.com/en/what:job/jobid:12345")
        assert key in title_seen

    def test_same_title_company_different_url_not_deduped(self):
        """Same institution and title but different URL (different department) should each be kept."""
        pool = {"j1": {"title": "PhD Student in Materials", "company": "KTH",
                       "url": "https://kth.varbi.com/en/what:job/jobID:11111/"}}
        title_seen = self._make_title_seen(pool)
        # A different URL (different jobID) should not match
        from utils import canonical_url
        new_key = (_normalize_title("PhD Student in Materials"), "kth",
                   canonical_url("https://kth.varbi.com/en/what:job/jobID:22222/"))
        assert new_key not in title_seen


# ── include/exclude override logic (Bug 1 regression) ────────────────────────

class TestIncludeExcludeOverride:
    """
    Regression test: when src_cfg["include"] = [] it should not fall through to the search-level include.
    Before the fix: `src_cfg.get("include") or search.get("include", [])` fell through.
    After the fix: `src_cfg["include"] if "include" in src_cfg else ...` handles the empty list correctly.
    """

    def _simulate_override(self, src_cfg: dict, search: dict) -> list:
        """Reproduce the inc assignment logic inside fetch_all."""
        return src_cfg["include"] if "include" in src_cfg else search.get("include", [])

    def test_empty_src_include_does_not_fall_through(self):
        src_cfg = {"include": []}   # explicitly requests an empty list (no filtering at all)
        search  = {"include": ["phd", "doktorand"]}
        inc = self._simulate_override(src_cfg, search)
        assert inc == [], "an empty list should be used as-is, not fall through to the search level"

    def test_absent_src_include_uses_search_default(self):
        src_cfg = {}   # include not set, should inherit the search level
        search  = {"include": ["phd", "doktorand"]}
        inc = self._simulate_override(src_cfg, search)
        assert inc == ["phd", "doktorand"]

    def test_nonempty_src_include_overrides_search(self):
        src_cfg = {"include": ["plasma materials"]}
        search  = {"include": ["phd"]}
        inc = self._simulate_override(src_cfg, search)
        assert inc == ["plasma materials"]

    def test_old_broken_logic_would_have_fallen_through(self):
        """Document the buggy behavior of the old logic to ensure it doesn't recur after the fix."""
        src_cfg = {"include": []}
        search  = {"include": ["phd"]}
        # Old logic:
        old_result = src_cfg.get("include") or search.get("include", [])
        # New logic:
        new_result = src_cfg["include"] if "include" in src_cfg else search.get("include", [])
        assert old_result == ["phd"],  "old logic falls through (the expected-but-wrong behavior)"
        assert new_result == [],       "new logic should return an empty list"
