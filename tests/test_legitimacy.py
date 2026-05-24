"""
tests/test_legitimacy.py — the 4-layer legitimacy filter.

All paid layers (web search, LLM judge) are mocked, so these run offline with no
ANTHROPIC_API_KEY.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import legitimacy as L
from legitimacy import (
    Flag, LegitimacyResult, PASS, REVIEW, REJECT,
    layer1_rules, layer4_aggregate, assess, assess_batch,
)


# ── fixtures ─────────────────────────────────────────────────────────────────

SCAM = {
    "id": "scam1",
    "title": "Work From Home Data Entry",
    "company": "",
    "description": ("No experience needed! Earn $900 per day. Pay a one-time "
                    "registration fee of $50 to get started. Contact us on WhatsApp."),
    "contact_email": "hr.recruit@gmail.com",
    "fetched_at": "2026-05-20",
}

LEGIT = {
    "id": "legit1",
    "title": "Backend Engineer",
    "company": "Acme Corp",
    "description": "Build and operate our APIs. Requirements: 3+ years Python.",
    "contact_email": "jobs@acme.com",
    "fetched_at": "2026-05-23",
}


# ── Layer 1: rule checks ─────────────────────────────────────────────────────

class TestLayer1Rules:
    def test_upfront_fee_is_red(self):
        flags, _ = layer1_rules(SCAM)
        codes = {f.code: f for f in flags}
        assert "upfront_fee" in codes
        assert codes["upfront_fee"].severity == "red"
        assert codes["upfront_fee"].source  # must quote evidence

    def test_salary_anomaly_is_red(self):
        flags, _ = layer1_rules(SCAM)
        assert any(f.code == "salary_anomaly" and f.severity == "red" for f in flags)

    def test_free_email_is_yellow(self):
        flags, _ = layer1_rules(SCAM)
        f = next((f for f in flags if f.code == "personal_email"), None)
        assert f is not None and f.severity == "yellow"
        assert "gmail.com" in f.source

    def test_missing_company_flag(self):
        flags, _ = layer1_rules(SCAM)
        assert any(f.code == "no_company" for f in flags)

    def test_clean_job_has_no_flags(self):
        flags, _ = layer1_rules(LEGIT)
        assert flags == []

    def test_stale_posting_flag(self):
        old = {**LEGIT, "fetched_at": "2026-01-01"}
        flags, raw = layer1_rules(old, {"stale_posting_days": 60})
        assert any(f.code == "stale_posting" for f in flags)
        assert raw["posting_age_days"] > 60

    def test_no_network_call(self):
        # layer1 must be pure/local — calling with no client must not raise.
        flags, raw = layer1_rules(LEGIT)
        assert isinstance(raw, dict)


# ── remote-only ──────────────────────────────────────────────────────────────

class TestRemoteOnly:
    ONSITE = {
        "id": "onsite1", "title": "Backend Engineer", "company": "Acme Corp",
        "description": "On-site in Berlin. Relocation required.",
    }
    REMOTE = {
        "id": "remote1", "title": "Backend Engineer", "company": "Acme Corp",
        "description": "Fully remote, work from home. 3+ years Python.",
    }

    def test_looks_remote_helper(self):
        from utils import looks_remote
        assert looks_remote(self.REMOTE) is True
        assert looks_remote(self.ONSITE) is False
        # source-provided flag wins over text
        assert looks_remote({"title": "x", "description": "no remote", "is_remote": True}) is True
        assert looks_remote({"title": "x", "description": "fully remote", "is_remote": False}) is False
        # hybrid without a strong remote claim is not remote
        assert looks_remote({"title": "x", "description": "Hybrid, 2 days remote"}) is False

    def test_remote_only_flags_onsite(self):
        flags, _ = layer1_rules(self.ONSITE, {"remote_only": True})
        assert any(f.code == "not_remote" and f.severity == "red" for f in flags)

    def test_remote_only_passes_remote(self):
        flags, _ = layer1_rules(self.REMOTE, {"remote_only": True})
        assert not any(f.code == "not_remote" for f in flags)

    def test_no_remote_flag_when_disabled(self):
        # without remote_only, an on-site job is fine
        flags, _ = layer1_rules(self.ONSITE, {})
        assert not any(f.code == "not_remote" for f in flags)

    def test_not_remote_is_hard_veto(self):
        red = [Flag("red", "not_remote", "not remote", "…on-site…")]
        r = layer4_aggregate("x", red, 9.0, [], [])
        assert r.verdict == REJECT
        assert r.score <= L.HARD_FLAG_SCORE_CAP


# ── Layer 4: aggregate & threshold ───────────────────────────────────────────

class TestLayer4Aggregate:
    def test_hard_veto_caps_score(self):
        red = [Flag("red", "upfront_fee", "fee", "…fee…")]
        r = layer4_aggregate("x", red, 9.5, [], [])
        assert r.score <= L.HARD_FLAG_SCORE_CAP
        assert r.verdict == REJECT

    def test_company_not_found_is_hard_veto(self):
        reds = [Flag("red", "company_not_found", "nope", "summary")]
        r = layer4_aggregate("x", [], 9.0, reds, [])
        assert r.verdict == REJECT
        assert r.score <= L.HARD_FLAG_SCORE_CAP

    def test_high_score_passes(self):
        greens = [Flag("green", "company_verified", "ok", "https://acme.com")]
        r = layer4_aggregate("x", [], 8.0, greens, [], {"pass_threshold": 7.0})
        assert r.verdict == PASS
        assert r.green_flags

    def test_middle_score_is_review(self):
        r = layer4_aggregate("x", [], 5.5, [], [],
                             {"pass_threshold": 7.0, "reject_threshold": 4.0})
        assert r.verdict == REVIEW

    def test_low_score_rejects(self):
        r = layer4_aggregate("x", [], 2.0, [], [], {"reject_threshold": 4.0})
        assert r.verdict == REJECT

    def test_non_veto_red_drags_score(self):
        reds = [Flag("red", "verify", "weird", "src")]
        r = layer4_aggregate("x", [], 8.0, [], reds, {"pass_threshold": 7.0})
        assert r.score == 7.0  # 8 - 1


# ── orchestration: assess ────────────────────────────────────────────────────

class TestAssess:
    def _patch_paid(self, monkeypatch, score=8.0, flags=None):
        monkeypatch.setattr(L, "layer2_verify",
                            lambda job, cfg, client=None: ([], {"skipped": "test"}))
        monkeypatch.setattr(L, "layer3_judge",
                            lambda job, l2, cfg, client=None: (score, flags or [], {"ok": True}))

    def test_scam_fast_fails_and_skips_paid(self, monkeypatch):
        # If fast_fail short-circuits, layer3 must NOT be called.
        called = {"l3": False}

        def fake_l3(job, l2, cfg, client=None):
            called["l3"] = True
            return 9.0, [], {}

        monkeypatch.setattr(L, "layer2_verify",
                            lambda job, cfg, client=None: ([], {}))
        monkeypatch.setattr(L, "layer3_judge", fake_l3)

        r = assess(SCAM, cfg={"fast_fail": True})
        assert r.verdict == REJECT
        assert r.score <= L.HARD_FLAG_SCORE_CAP
        assert called["l3"] is False
        assert "skipped" in r.layers["layer3"]

    def test_legit_job_passes(self, monkeypatch):
        self._patch_paid(monkeypatch, score=8.0,
                         flags=[Flag("green", "ok", "looks fine", "https://acme.com")])
        r = assess(LEGIT, cfg={"pass_threshold": 7.0, "fast_fail": True})
        assert r.verdict == PASS
        assert r.passed

    def test_result_serializes(self, monkeypatch):
        self._patch_paid(monkeypatch, score=8.0)
        r = assess(LEGIT, cfg={"fast_fail": True})
        d = r.to_dict()
        assert set(d) >= {"job_id", "verdict", "score", "flags", "layers"}

    def test_batch(self, monkeypatch):
        self._patch_paid(monkeypatch, score=8.0)
        results = assess_batch([LEGIT, dict(LEGIT, id="legit2")],
                               cfg={"fast_fail": True}, throttle=0)
        assert len(results) == 2
        assert all(isinstance(r, LegitimacyResult) for r in results)


# ── flags always carry a source (explainability contract) ────────────────────

class TestExplainability:
    def test_every_layer1_red_flag_has_source(self):
        flags, _ = layer1_rules(SCAM)
        for f in flags:
            if f.severity == "red":
                assert f.source, f"red flag {f.code} must quote a source"
