"""
legitimacy.py — Job-posting legitimacy filter (4 layers).

Answers a different question than the fit-scorer: not "does this job match me?"
but "is this job real, legal, and not a scam?". A posting must clear this filter
before it is ever pushed to the user.

Pipeline
--------
Layer 1  rule checks          fast & free, local heuristics; a hard red flag here
                              caps the legitimacy score (fast fail, no LLM cost).
Layer 2  external verification  Claude web_search: is the company on its official
                              site? does it have a Glassdoor/review history? is the
                              recruiter real? Every finding quotes its source.
Layer 3  LLM judge            scores legitimacy / real-remote / ghost-job risk, with
                              evidence. Every flag must quote the source — explainable.
Layer 4  aggregate & threshold  rules hard-veto, the LLM soft-scores; combine into a
                              verdict. Output: score + red/green flags.

The public entry point is `assess(job, cfg)` returning a `LegitimacyResult`, and
`assess_batch(jobs, cfg)` for a list. Each layer is independently testable.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse


# ── verdict vocabulary ──────────────────────────────────────────────────────
PASS = "pass"          # legitimate, push to user
REVIEW = "review"      # ambiguous, surface but flag for human judgement
REJECT = "reject"      # likely illegitimate / scam, do not push

# A Layer-1 hard red flag caps the final score at this ceiling no matter what the
# LLM says — rules hard-veto.
HARD_FLAG_SCORE_CAP = 3.0

# Default verdict thresholds on the 0–10 legitimacy score (overridable via config).
DEFAULT_PASS_THRESHOLD = 7.0
DEFAULT_REJECT_THRESHOLD = 4.0


@dataclass
class Flag:
    """One explainable signal. `severity` is 'red' | 'yellow' | 'green'."""
    severity: str
    code: str
    message: str
    source: str = ""   # quoted evidence: a URL, a phrase from the posting, etc.

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LegitimacyResult:
    job_id: str
    verdict: str = REVIEW
    score: float = 0.0                       # 0–10, higher = more clearly legitimate
    flags: list[Flag] = field(default_factory=list)
    layers: dict = field(default_factory=dict)  # per-layer raw output for auditing

    @property
    def red_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == "red"]

    @property
    def green_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.severity == "green"]

    @property
    def passed(self) -> bool:
        return self.verdict == PASS

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "verdict": self.verdict,
            "score": round(self.score, 1),
            "flags": [f.to_dict() for f in self.flags],
            "layers": self.layers,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Layer 1 — rule checks (local, free, fast)
# ─────────────────────────────────────────────────────────────────────────────

# Free-mail providers: a recruiter contact at one of these is a yellow flag for a
# company role (legitimate employers use a corporate domain).
_FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
    "icloud.com", "protonmail.com", "mail.com", "gmx.com", "yandex.com",
    "163.com", "qq.com", "126.com",
}

# Phrases that strongly correlate with advance-fee / payment scams.
_UPFRONT_FEE_PATTERNS = [
    r"\bregistration fee\b", r"\bprocessing fee\b", r"\btraining fee\b",
    r"\bsecurity deposit\b", r"\bpay (?:a|an|the)?\s*fee\b",
    r"\bsend (?:money|payment|\$)", r"\bwire transfer\b",
    r"\bpurchase (?:a )?(?:starter )?kit\b", r"\bbuy your own equipment up front\b",
    r"\bgift card", r"\bcryptocurrency\b.*\bpayment\b",
]

# Phrases typical of low-effort scam / MLM / "too good to be true" postings.
_SCAM_LANGUAGE_PATTERNS = [
    r"\bno experience (?:needed|required|necessary)\b.*\$\s*\d{3,}",
    r"\bwork from home\b.*\$\d{3,}\s*(?:per|/)\s*day",
    r"\bunlimited (?:earning|income) potential\b",
    r"\bbe your own boss\b", r"\bmillionaire\b",
    r"\bguaranteed (?:income|job|placement)\b",
    r"\bstart (?:earning )?(?:today|immediately)\b.*\$",
    r"\btext (?:us )?(?:at )?\+?\d", r"\bwhatsapp\b.*\b(?:job|position|hr)\b",
]

# email regex
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
_SALARY_RE = re.compile(
    r"(?:\$|usd|eur|gbp|sek|€|£)\s?([\d][\d,\.]{2,})", re.IGNORECASE)


def _find_email_domains(text: str) -> list[str]:
    return [m.lower() for m in _EMAIL_RE.findall(text or "")]


def _posting_age_days(job: dict) -> int | None:
    """Days since the posting was first seen / published, if a date is available."""
    raw = job.get("posted_at") or job.get("fetched_at") or ""
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d"):
        try:
            d = datetime.strptime(raw[: len(fmt) + 2], fmt).date()
            return (date.today() - d).days
        except ValueError:
            continue
    return None


def layer1_rules(job: dict, cfg: dict | None = None) -> tuple[list[Flag], dict]:
    """Local heuristics. Returns (flags, raw) and never makes a network call.

    Checks: upfront fees, salary anomaly, personal/free email, posting age,
    obvious scam language. A red flag here caps the final score downstream.
    """
    cfg = cfg or {}
    flags: list[Flag] = []
    text = " ".join(
        str(job.get(k, "")) for k in ("title", "company", "description")
    )
    text_l = text.lower()
    raw: dict = {}

    # --- upfront fees -------------------------------------------------------
    for pat in _UPFRONT_FEE_PATTERNS:
        m = re.search(pat, text_l)
        if m:
            flags.append(Flag(
                "red", "upfront_fee",
                "Posting asks the applicant to pay money — a hallmark of advance-fee scams.",
                source=_quote(text, m.start()),
            ))
            break

    # --- scam language ------------------------------------------------------
    for pat in _SCAM_LANGUAGE_PATTERNS:
        m = re.search(pat, text_l)
        if m:
            flags.append(Flag(
                "yellow", "scam_language",
                "Posting uses language typical of MLM / get-rich-quick schemes.",
                source=_quote(text, m.start()),
            ))
            break

    # --- salary anomaly -----------------------------------------------------
    salaries = [_to_number(s) for s in _SALARY_RE.findall(text)]
    salaries = [s for s in salaries if s is not None]
    if salaries:
        hi = max(salaries)
        raw["max_salary_figure"] = hi
        # An hourly/daily figure paired with "per day"/"daily" that implies a
        # wildly high annualised number is a classic bait.
        per_day = re.search(r"\$?\s*[\d,]+\s*(?:per|/)\s*(?:day|hour)", text_l)
        if per_day and hi >= 500 and "no experience" in text_l:
            flags.append(Flag(
                "red", "salary_anomaly",
                "Salary is implausibly high for a role advertised as requiring no experience.",
                source=_quote(text, per_day.start()),
            ))

    # --- personal / free email contact -------------------------------------
    contact = (job.get("contact_email") or "").lower()
    domains = _find_email_domains(text)
    if contact:
        cd = contact.split("@")[-1]
        if cd:
            domains.append(cd)
    free_hit = next((d for d in domains if d in _FREE_EMAIL_DOMAINS), None)
    if free_hit:
        flags.append(Flag(
            "yellow", "personal_email",
            f"Recruiter contact uses a free-mail domain ({free_hit}); legitimate "
            f"employers normally use a corporate domain.",
            source=free_hit,
        ))
    raw["contact_domains"] = sorted(set(domains))

    # --- posting age --------------------------------------------------------
    age = _posting_age_days(job)
    if age is not None:
        raw["posting_age_days"] = age
        stale_after = cfg.get("stale_posting_days", 60)
        if age > stale_after:
            flags.append(Flag(
                "yellow", "stale_posting",
                f"Posting is {age} days old (> {stale_after}); may be a ghost job "
                f"that is never actually filled.",
                source=job.get("posted_at") or job.get("fetched_at", ""),
            ))

    # --- missing company ----------------------------------------------------
    if not (job.get("company") or "").strip():
        flags.append(Flag(
            "yellow", "no_company",
            "Posting does not name a hiring company.",
            source="",
        ))

    raw["flag_codes"] = [f.code for f in flags]
    return flags, raw


def _quote(text: str, idx: int, span: int = 90) -> str:
    """Return a short quoted excerpt of `text` around character index `idx`."""
    start = max(0, idx - 10)
    snippet = text[start: start + span].strip()
    return f"…{snippet}…" if snippet else ""


def _to_number(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except (ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Layer 2 — external verification (Claude web_search)
# ─────────────────────────────────────────────────────────────────────────────

_VERIFY_SYSTEM = """You verify whether a job posting comes from a real, identifiable employer.

Use web search to check, for the company and role given:
1. Does the company have an official website / public footprint that matches the posting?
2. Is there a review/employer history (Glassdoor, LinkedIn, news, registry)?
3. If a recruiter or contact is named, do they plausibly belong to that company?
4. Is this exact role listed on the company's own careers page?

Rules:
- Quote the SOURCE (a URL or the exact text you saw) for every claim. No source = do not claim it.
- If you cannot find evidence either way, say so plainly. Absence of evidence is not proof of fraud.
- Be concise and neutral; you are gathering evidence, not making the final verdict.

Return ONLY a JSON object:
{
  "company_found": true | false | null,
  "official_site": "url or empty",
  "review_history": "one-line summary with source, or empty",
  "role_on_company_site": true | false | null,
  "recruiter_plausible": true | false | null,
  "findings": [
    {"severity": "green|yellow|red", "message": "...", "source": "url or quoted text"}
  ],
  "summary": "2-3 sentence neutral summary"
}"""


def layer2_verify(job: dict, cfg: dict, client=None) -> tuple[list[Flag], dict]:
    """External verification via Claude's built-in web_search tool.

    Skipped (returns an inconclusive placeholder) when disabled in config or when
    no client/key is available, so the rest of the pipeline still runs.
    """
    l2cfg = cfg.get("layer2", {})
    if not l2cfg.get("enabled", True):
        return [], {"skipped": "disabled"}

    if client is None:
        from utils import get_client
        try:
            client = get_client()
        except Exception as e:  # no API key, offline, etc.
            return [], {"skipped": f"no_client: {e}"}

    user = (
        f"Job title: {job.get('title','')}\n"
        f"Company: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\n"
        f"Posting URL: {job.get('url','')}\n"
        f"Contact: {job.get('contact_email','')}\n\n"
        f"Description (truncated):\n{(job.get('description','') or '')[:1500]}"
    )

    model = l2cfg.get("model", cfg.get("model", "claude-sonnet-4-6"))
    max_uses = l2cfg.get("max_searches", 4)

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=l2cfg.get("max_tokens", 1500),
            system=_VERIFY_SYSTEM,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }],
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        return [], {"skipped": f"web_search_failed: {e}"}

    data = _extract_json_obj(resp)
    if data is None:
        return [], {"skipped": "no_json", "raw_text": _all_text(resp)[:500]}

    flags: list[Flag] = []
    for f in data.get("findings", []):
        sev = f.get("severity", "yellow")
        if sev not in ("red", "yellow", "green"):
            sev = "yellow"
        flags.append(Flag(
            sev, "verify",
            f.get("message", ""),
            source=f.get("source", ""),
        ))

    # Promote the headline structured signals into flags when decisive.
    if data.get("company_found") is False:
        flags.append(Flag(
            "red", "company_not_found",
            "No official website or public footprint found for the named company.",
            source=data.get("summary", ""),
        ))
    elif data.get("official_site"):
        flags.append(Flag(
            "green", "company_verified",
            "Company has a matching official website / public footprint.",
            source=data["official_site"],
        ))
    if data.get("role_on_company_site") is True:
        flags.append(Flag(
            "green", "role_on_site",
            "This role is listed on the company's own careers page.",
            source=data.get("official_site", ""),
        ))

    return flags, data


# ─────────────────────────────────────────────────────────────────────────────
# Layer 3 — LLM judge (legitimacy / real-remote / ghost-job, with evidence)
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_JUDGE_PROMPT = "data/prompts/legitimacy_judge.md"


def layer3_judge(job: dict, layer2: dict, cfg: dict, client=None) -> tuple[float, list[Flag], dict]:
    """Ask the LLM to score legitimacy with evidence. Returns (score, flags, raw)."""
    l3cfg = cfg.get("layer3", {})
    prompt_path = Path(l3cfg.get("prompt", _DEFAULT_JUDGE_PROMPT))
    system_prompt = prompt_path.read_text(encoding="utf-8") if prompt_path.exists() \
        else _BUILTIN_JUDGE_PROMPT

    if client is None:
        from utils import get_client
        try:
            client = get_client()
        except Exception as e:
            return 5.0, [], {"skipped": f"no_client: {e}"}

    user = (
        f"POSTING\n"
        f"Title: {job.get('title','')}\n"
        f"Company: {job.get('company','')}\n"
        f"Location: {job.get('location','')}\n"
        f"URL: {job.get('url','')}\n"
        f"Contact: {job.get('contact_email','')}\n"
        f"Description:\n{(job.get('description','') or '')[:3000]}\n\n"
        f"EXTERNAL VERIFICATION (Layer 2 findings)\n"
        f"{json.dumps(layer2, ensure_ascii=False)[:2000]}"
    )

    model = l3cfg.get("model", cfg.get("model", "claude-sonnet-4-6"))
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=l3cfg.get("max_tokens", 1200),
            system=system_prompt,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        return 5.0, [], {"skipped": f"judge_failed: {e}"}

    data = _extract_json_obj(resp)
    if data is None:
        return 5.0, [], {"skipped": "no_json", "raw_text": _all_text(resp)[:500]}

    score = _clamp(_to_number(str(data.get("legitimacy_score", 5))) or 5.0, 0, 10)
    flags: list[Flag] = []
    for f in data.get("flags", []):
        sev = f.get("severity", "yellow")
        if sev not in ("red", "yellow", "green"):
            sev = "yellow"
        flags.append(Flag(
            sev, f.get("code", "judge"),
            f.get("message", ""),
            source=f.get("source", ""),
        ))
    return score, flags, data


# Fallback prompt used if the prompt file is missing (keeps the module runnable
# stand-alone). The shipped file at data/prompts/legitimacy_judge.md is canonical.
_BUILTIN_JUDGE_PROMPT = """You judge whether a job posting is legitimate.

Score three risks and combine them into one legitimacy_score (0-10, higher = more
clearly legitimate and safe to apply to):
- scam / fraud risk (advance fees, identity harvesting, fake employer)
- ghost-job risk (posting that will never be filled — perpetual, vague, stale)
- "real remote" honesty (does a remote claim match reality / region limits?)

EVERY flag MUST quote its source: a URL from the verification findings, or the exact
phrase from the posting. A claim with no source is not allowed.

Return ONLY JSON:
{
  "legitimacy_score": 0-10,
  "scam_risk": "low|medium|high",
  "ghost_job_risk": "low|medium|high",
  "remote_honesty": "honest|misleading|n/a",
  "flags": [
    {"severity": "red|yellow|green", "code": "short_code", "message": "...", "source": "url or quoted phrase"}
  ],
  "summary": "2-3 sentences"
}"""


# ─────────────────────────────────────────────────────────────────────────────
# Layer 4 — aggregate & threshold
# ─────────────────────────────────────────────────────────────────────────────

def layer4_aggregate(job_id: str, l1_flags, l3_score, l2_flags, l3_flags,
                     cfg: dict | None = None) -> LegitimacyResult:
    """Combine rule veto + LLM soft-score into a final verdict.

    Rules hard-veto: any Layer-1 red flag caps the score at HARD_FLAG_SCORE_CAP.
    A red flag from external verification (e.g. company not found) also caps it.
    """
    cfg = cfg or {}
    pass_t = cfg.get("pass_threshold", DEFAULT_PASS_THRESHOLD)
    reject_t = cfg.get("reject_threshold", DEFAULT_REJECT_THRESHOLD)

    flags = list(l1_flags) + list(l2_flags) + list(l3_flags)
    score = float(l3_score)

    hard_veto_codes = {"upfront_fee", "salary_anomaly", "company_not_found"}
    has_hard_veto = any(
        f.severity == "red" and f.code in hard_veto_codes for f in flags
    )
    if has_hard_veto:
        score = min(score, HARD_FLAG_SCORE_CAP)

    # Each non-vetoing red flag still drags the score down a little.
    other_reds = sum(
        1 for f in flags if f.severity == "red" and f.code not in hard_veto_codes
    )
    score = _clamp(score - other_reds, 0, 10)

    if score >= pass_t and not has_hard_veto:
        verdict = PASS
    elif score < reject_t or has_hard_veto:
        verdict = REJECT
    else:
        verdict = REVIEW

    return LegitimacyResult(
        job_id=job_id,
        verdict=verdict,
        score=score,
        flags=flags,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────

def assess(job: dict, cfg: dict | None = None, client=None) -> LegitimacyResult:
    """Run all four layers on one job and return a LegitimacyResult."""
    cfg = cfg or {}
    job_id = job.get("id", "")

    l1_flags, l1_raw = layer1_rules(job, cfg)

    # Fast fail: if Layer 1 already found a hard red flag, we can skip the paid
    # network/LLM layers entirely (rules are "fast & free").
    l1_hard = any(
        f.severity == "red" and f.code in {"upfront_fee", "salary_anomaly"}
        for f in l1_flags
    )
    skip_paid = l1_hard and cfg.get("fast_fail", True)

    if skip_paid:
        l2_flags, l2_raw = [], {"skipped": "layer1_hard_fail"}
        l3_score, l3_flags, l3_raw = HARD_FLAG_SCORE_CAP, [], {"skipped": "layer1_hard_fail"}
    else:
        l2_flags, l2_raw = layer2_verify(job, cfg, client=client)
        l3_score, l3_flags, l3_raw = layer3_judge(job, l2_raw, cfg, client=client)

    result = layer4_aggregate(
        job_id, l1_flags, l3_score, l2_flags, l3_flags, cfg=cfg
    )
    result.layers = {
        "layer1": l1_raw,
        "layer2": l2_raw,
        "layer3": l3_raw,
    }
    return result


def assess_batch(jobs: list[dict], cfg: dict | None = None,
                 client=None, throttle: float = 2.0) -> list[LegitimacyResult]:
    """Assess a list of jobs sequentially, throttling between paid calls."""
    cfg = cfg or {}
    results: list[LegitimacyResult] = []
    for i, job in enumerate(jobs):
        results.append(assess(job, cfg=cfg, client=client))
        used_paid = "skipped" not in results[-1].layers.get("layer3", {})
        if used_paid and i < len(jobs) - 1 and throttle:
            time.sleep(throttle)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _all_text(resp) -> str:
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _extract_json_obj(resp) -> dict | None:
    """Pull the first JSON object out of an Anthropic response's text blocks."""
    text = _all_text(resp).strip()
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        return None
    try:
        return json.loads(text[start:end])
    except json.JSONDecodeError:
        return None
