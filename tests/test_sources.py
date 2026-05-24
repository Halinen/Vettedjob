"""
tests/test_sources.py — Pure-function unit tests, no network dependency.
"""
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from sources import (
    SOURCE_REGISTRY,
    _match_any,
    _stable_id,
    _varbi_id,
    canonical_url,
)


# ── canonical_url ─────────────────────────────────────────────────────────────

class TestCanonicalUrl:
    def test_strips_trailing_slash(self):
        assert canonical_url("https://example.com/path/") == "https://example.com/path"

    def test_lowercases(self):
        assert canonical_url("HTTPS://Example.COM/Path") == "https://example.com/path"

    def test_strips_tracking_params_and_fragment(self):
        result = canonical_url("https://example.com/page?utm_source=email&utm_medium=cpc#section")
        assert result == "https://example.com/page"

    def test_preserves_business_params(self):
        """Business params like rmjob should be preserved, to avoid different LiU job URLs being wrongly merged"""
        url = "https://liu.se/en/work-at-liu/vacancies/?rmjob=14533&rmlang=UK"
        result = canonical_url(url)
        assert "rmjob=14533" in result

    def test_strips_tracking_keeps_business(self):
        url = "https://liu.se/vacancies/?rmjob=14533&utm_source=email"
        result = canonical_url(url)
        assert "rmjob=14533" in result
        assert "utm_source" not in result

    def test_no_double_slash_removal(self):
        result = canonical_url("https://example.com/a/b")
        assert result == "https://example.com/a/b"

    def test_empty_path(self):
        result = canonical_url("https://example.com/")
        assert result == "https://example.com"


# ── _stable_id ────────────────────────────────────────────────────────────────

class TestStableId:
    def test_format(self):
        sid = _stable_id("af", "https://example.com/job/123")
        assert sid.startswith("af_")
        assert len(sid) == len("af_") + 12

    def test_deterministic(self):
        url = "https://example.com/job/456"
        assert _stable_id("spy", url) == _stable_id("spy", url)

    def test_different_urls_differ(self):
        assert _stable_id("af", "https://a.com/1") != _stable_id("af", "https://a.com/2")

    def test_normalizes_before_hashing(self):
        # Trailing slash should produce same id
        assert _stable_id("af", "https://a.com/job") == _stable_id("af", "https://a.com/job/")


# ── _varbi_id ─────────────────────────────────────────────────────────────────

class TestVarbiId:
    def test_extracts_jobid_from_url(self):
        url = "https://kth.varbi.com/en/what:job/jobID:12345/where:4/"
        assert _varbi_id(url) == "varbi_12345"

    def test_falls_back_to_hash_when_no_jobid(self):
        url = "https://kth.varbi.com/en/some/other/path"
        vid = _varbi_id(url)
        assert vid.startswith("varbi_")
        assert len(vid) == len("varbi_") + 12

    def test_consistent_fallback(self):
        url = "https://kth.varbi.com/no-id"
        assert _varbi_id(url) == _varbi_id(url)


# ── _match_any ────────────────────────────────────────────────────────────────

class TestMatchAny:
    def test_basic_match(self):
        assert _match_any("PhD position in physics", ["phd"]) is True

    def test_case_insensitive(self):
        assert _match_any("Doktorand i fysik", ["DOKTORAND"]) is True

    def test_no_match(self):
        assert _match_any("Engineer role at Ericsson", ["phd", "doktorand"]) is False

    def test_empty_words_list(self):
        assert _match_any("any text", []) is False

    def test_empty_text(self):
        assert _match_any("", ["phd"]) is False

    def test_partial_word_match(self):
        # "postdoc" contains "doc" — make sure substring matching works as designed
        assert _match_any("postdoc position", ["doc"]) is True

    def test_multiple_words_any_hit(self):
        assert _match_any("licentiat program", ["phd", "doctoral", "licentiat"]) is True

    def test_exclude_hit(self):
        # Simulate exclude logic: if match_any returns True for exclude list, discard
        assert _match_any("medicin doktorand", ["medicin", "biologi"]) is True


# ── SOURCE_REGISTRY ───────────────────────────────────────────────────────────

class TestSourceRegistry:
    def test_all_expected_sources_registered(self):
        expected = {
            "varbi", "liu", "chalmers", "af", "jobspy",
            "euraxess", "academic_positions", "excillum", "qamcom",
            "abb", "atlas_copco", "englishjobs", "rise", "swerim",
        }
        assert expected == set(SOURCE_REGISTRY.keys())

    def test_all_values_are_callable(self):
        for name, fn in SOURCE_REGISTRY.items():
            assert callable(fn), f"{name} is not callable"
