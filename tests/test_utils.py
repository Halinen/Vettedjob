"""
tests/test_utils.py — Tests for functions in utils.py that don't depend on external APIs.
"""
import csv
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from utils import build_seen_ids, canonical_url


# ── canonical_url ─────────────────────────────────────────────────────────────

class TestCanonicalUrl:
    def test_strips_trailing_slash(self):
        assert canonical_url("https://example.com/path/") == "https://example.com/path"

    def test_lowercases(self):
        assert canonical_url("HTTPS://EXAMPLE.COM/PATH") == "https://example.com/path"

    def test_strips_tracking_params(self):
        result = canonical_url("https://example.com/page?utm_source=email&utm_medium=cpc")
        assert result == "https://example.com/page"

    def test_preserves_non_tracking_params(self):
        result = canonical_url("https://example.com/page?rmjob=123&rmlang=UK")
        assert "rmjob=123" in result

    def test_strips_fragment(self):
        result = canonical_url("https://example.com/page#section")
        assert result == "https://example.com/page"

    def test_consistent_with_and_without_slash(self):
        assert canonical_url("https://a.com/job") == canonical_url("https://a.com/job/")

    def test_whitespace_stripped(self):
        assert canonical_url("  https://a.com/j  ") == "https://a.com/j"


# ── build_seen_ids ────────────────────────────────────────────────────────────

class TestBuildSeenIds:
    def test_collects_pool_ids(self, tmp_path):
        pool = {"id_a": {"title": "A"}, "id_b": {"title": "B"}}
        pool_file = tmp_path / "pool.json"
        pool_file.write_text(json.dumps(pool))
        seen = build_seen_ids(str(pool_file), str(tmp_path / "noexist.csv"))
        assert "id_a" in seen
        assert "id_b" in seen

    def test_collects_eval_log_ids(self, tmp_path):
        eval_file = tmp_path / "eval_log.csv"
        with eval_file.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "title"])
            writer.writeheader()
            writer.writerow({"id": "log_id_1", "title": "T"})
        seen = build_seen_ids(str(tmp_path / "nopool.json"), str(eval_file))
        assert "log_id_1" in seen

    def test_union_of_both_sources(self, tmp_path):
        pool_file = tmp_path / "pool.json"
        pool_file.write_text(json.dumps({"pool_x": {}}))
        eval_file = tmp_path / "eval_log.csv"
        with eval_file.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id"])
            writer.writeheader()
            writer.writerow({"id": "log_y"})
        seen = build_seen_ids(str(pool_file), str(eval_file))
        assert "pool_x" in seen
        assert "log_y" in seen

    def test_missing_pool_file_still_works(self, tmp_path):
        seen = build_seen_ids(str(tmp_path / "nopool.json"),
                               str(tmp_path / "nolog.csv"))
        assert isinstance(seen, set)
        assert len(seen) == 0

    def test_missing_eval_log_still_works(self, tmp_path):
        pool_file = tmp_path / "pool.json"
        pool_file.write_text(json.dumps({"only_id": {}}))
        seen = build_seen_ids(str(pool_file), str(tmp_path / "nolog.csv"))
        assert "only_id" in seen

    def test_returns_set_type(self, tmp_path):
        seen = build_seen_ids(str(tmp_path / "a.json"), str(tmp_path / "b.csv"))
        assert isinstance(seen, set)

    def test_eval_log_missing_id_column_skipped(self, tmp_path):
        """Rows in eval_log with an empty id should not enter the seen set."""
        eval_file = tmp_path / "eval_log.csv"
        with eval_file.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "title"])
            writer.writeheader()
            writer.writerow({"id": "", "title": "no id"})
            writer.writerow({"id": "real_id", "title": "has id"})
        seen = build_seen_ids(str(tmp_path / "nopool.json"), str(eval_file))
        assert "" not in seen
        assert "real_id" in seen
