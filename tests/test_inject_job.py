"""
tests/test_inject_job.py — Tests for the queue operation logic in inject_job.py.
"""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import inject_job
from inject_job import (
    _url_exists,
    inject_one,
    make_id,
    show_queue,
    clear_queue,
)


# ── make_id ───────────────────────────────────────────────────────────────────

class TestMakeId:
    def test_format_prefix(self):
        jid = make_id("https://example.com/job/1")
        assert jid.startswith("manual_")

    def test_deterministic(self):
        url = "https://example.com/job/42"
        assert make_id(url) == make_id(url)

    def test_different_urls_differ(self):
        assert make_id("https://a.com/1") != make_id("https://a.com/2")

    def test_normalizes_url_before_hashing(self):
        # trailing slash should produce same id
        assert make_id("https://a.com/job") == make_id("https://a.com/job/")

    def test_length(self):
        jid = make_id("https://a.com/x")
        assert len(jid) == len("manual_") + 12


# ── _url_exists ───────────────────────────────────────────────────────────────

class TestUrlExists:
    def test_url_in_queue(self):
        queue = [{"url": "https://example.com/job/1", "processed": False}]
        assert _url_exists("https://example.com/job/1", queue) is True

    def test_url_not_in_queue_no_pool(self, tmp_path):
        with patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            result = _url_exists("https://example.com/job/999", [])
        assert result is False

    def test_url_normalized_match(self):
        """A trailing-slash difference in the URL should not affect dedup detection."""
        queue = [{"url": "https://example.com/job/1/", "processed": False}]
        assert _url_exists("https://example.com/job/1", queue) is True

    def test_url_in_pool(self, tmp_path):
        pool = {"some_id": {"url": "https://pooled.com/job/5", "title": "T"}}
        pool_file = tmp_path / "pool.json"
        pool_file.write_text(json.dumps(pool))
        with patch.object(inject_job, "POOL_PATH", pool_file):
            result = _url_exists("https://pooled.com/job/5", [])
        assert result is True

    def test_different_url_not_found(self, tmp_path):
        pool = {"id1": {"url": "https://other.com/job/1"}}
        pool_file = tmp_path / "pool.json"
        pool_file.write_text(json.dumps(pool))
        with patch.object(inject_job, "POOL_PATH", pool_file):
            result = _url_exists("https://nowhere.com/job/99", [])
        assert result is False


# ── inject_one ────────────────────────────────────────────────────────────────

def _load_queue_file(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def empty_queue(tmp_path):
    """Empty queue file that inject_one will read/write."""
    q = tmp_path / "inject_queue.json"
    q.write_text("[]")
    return q, tmp_path


class TestInjectOne:
    def test_adds_entry_to_queue(self, empty_queue):
        q_file, tmp_path = empty_queue
        job = {"type": "job", "title": "RF Engineer", "company": "Ericsson",
               "url": "https://ericsson.com/job/1", "description": "test"}
        with patch.object(inject_job, "QUEUE_PATH", q_file), \
             patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            inject_one(job)
        items = _load_queue_file(q_file)
        assert len(items) == 1
        assert items[0]["title"] == "RF Engineer"
        assert items[0]["processed"] is False

    def test_duplicate_url_skipped(self, empty_queue):
        q_file, tmp_path = empty_queue
        job = {"type": "job", "title": "RF Engineer", "company": "Ericsson",
               "url": "https://ericsson.com/job/1", "description": ""}
        with patch.object(inject_job, "QUEUE_PATH", q_file), \
             patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            inject_one(job)
            inject_one(job)  # second call with same URL
        items = _load_queue_file(q_file)
        assert len(items) == 1

    def test_manual_score_none_when_absent(self, empty_queue):
        q_file, tmp_path = empty_queue
        job = {"type": "phd", "title": "PhD", "company": "KTH",
               "url": "https://kth.se/job/2", "description": ""}
        with patch.object(inject_job, "QUEUE_PATH", q_file), \
             patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            inject_one(job)
        items = _load_queue_file(q_file)
        assert items[0]["manual_score"] is None

    def test_manual_score_preserved(self, empty_queue):
        q_file, tmp_path = empty_queue
        job = {"type": "phd", "title": "PhD", "company": "KTH",
               "url": "https://kth.se/job/3", "description": "", "manual_score": 9}
        with patch.object(inject_job, "QUEUE_PATH", q_file), \
             patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            inject_one(job)
        items = _load_queue_file(q_file)
        assert items[0]["manual_score"] == 9

    def test_id_is_stable_manual_prefix(self, empty_queue):
        q_file, tmp_path = empty_queue
        job = {"type": "job", "title": "T", "company": "C",
               "url": "https://example.com/j/7", "description": ""}
        with patch.object(inject_job, "QUEUE_PATH", q_file), \
             patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            inject_one(job)
        items = _load_queue_file(q_file)
        assert items[0]["id"].startswith("manual_")

    def test_default_application_method_is_web(self, empty_queue):
        q_file, tmp_path = empty_queue
        job = {"type": "job", "title": "T", "company": "C",
               "url": "https://example.com/j/8", "description": ""}
        with patch.object(inject_job, "QUEUE_PATH", q_file), \
             patch.object(inject_job, "POOL_PATH", tmp_path / "pool.json"):
            inject_one(job)
        items = _load_queue_file(q_file)
        assert items[0]["application_method"] == "web"


# ── clear_queue ───────────────────────────────────────────────────────────────

class TestClearQueue:
    def test_removes_unprocessed_keeps_processed(self, tmp_path):
        q_file = tmp_path / "inject_queue.json"
        items = [
            {"id": "m1", "processed": False, "title": "A"},
            {"id": "m2", "processed": True,  "title": "B"},
        ]
        q_file.write_text(json.dumps(items))
        with patch.object(inject_job, "QUEUE_PATH", q_file):
            clear_queue()
        remaining = _load_queue_file(q_file)
        assert len(remaining) == 1
        assert remaining[0]["id"] == "m2"

    def test_all_processed_nothing_removed(self, tmp_path):
        q_file = tmp_path / "inject_queue.json"
        items = [{"id": "m1", "processed": True, "title": "A"}]
        q_file.write_text(json.dumps(items))
        with patch.object(inject_job, "QUEUE_PATH", q_file):
            clear_queue()
        remaining = _load_queue_file(q_file)
        assert len(remaining) == 1
