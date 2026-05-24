<!-- NOTE: may reference legacy behavior -->
# Job Search Toolkit — Optimization Plan (2026-04 revision)

An assessment and plan based on the actual current state of the code. The system architecture is stable, so the following focuses on concrete, actionable improvements.

---

## Current State at a Glance

**Completed and well designed**

- `eval_log.csv` is append-only and never modified; history is immutable ✅
- Clear separation of cloud/local responsibilities (`cloud_run.py` vs the local GUI) ✅
- `searches/*.json` drives the data sources; changing config does not require changing code ✅
- `pool.json` is auto-cleaned with a 30-day TTL ✅
- Varbi RSS is implemented (9 universities) ✅
- LiU is fetched via the official RSS `liu.se/rss/liu-jobs-en.rss`, with the `rmjob` parameter extracting the job ID ✅
- Chalmers uses playwright to scrape the listing page + httpx to scrape details ✅
- `_varbi_id()` correctly extracts `jobID:XXXXXX` as a stable ID ✅
- Unit tests cover the core modules (sources/fetch/evaluate/inject/workspace) ✅
- `canonical_url` is now imported from `utils` in `sources.py` (**not three duplicate definitions**; the description in `testing.md` is outdated) ✅

**Known issues (to be fixed)**

1. Module-level Anthropic client initialization in `utils.py` causes non-API scripts to fail on import
2. `canonical_url` removes all query params, which can cause some Varbi URLs to be incorrectly merged (see below)
3. `fetch_chalmers` still depends on the migrated `web103.reachmee.com` (now 404)
4. `title + company` dedup is too aggressive and can wrongly suppress different positions from the same organization
5. No CI to run tests automatically

---

## Issue 1: Module-level client initialization in `utils.py`

**Problem**: `utils.py` runs `os.environ["ANTHROPIC_API_KEY"]` at the top level, causing
non-API scripts like `inject_job.py` and `sync_index.py` to fail on import when the environment variable is not set.
Tests have to use `os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")` to work around this.

**Fix**: change the client to lazy initialization.

```python
# utils.py — replace module-level initialization
_client = None

def get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return _client
```

Change all places that call `client.messages.create(...)` to `get_client().messages.create(...)`.
**Scope**: `utils.py::match_jobs`; other scripts are unaffected.
**Priority**: high; affects test reliability and script usability.

---

## Issue 2: `canonical_url` removes all query params

**Problem**: the current implementation removes the entire query string, causing the following URLs to be treated as identical:

```
https://kth.varbi.com/en/what:job/jobID:12345/  ← correct (path contains the ID)
https://uu.varbi.com/en/what:job/jobID:12345/   ← correct (different domain)
```

Varbi's URL structure already encodes the job ID in the path (`jobID:XXXXX/`), so it is unaffected by this issue.
But LiU's Reachmee URLs look like:

```
https://liu.se/en/work-at-liu/vacancies/?rmjob=14533&rmlang=UK
```

`canonical_url` simplifies this to `https://liu.se/en/work-at-liu/vacancies`,
collapsing all distinct job URLs into the same canonical URL, so LiU job dedup fails entirely.

`fetch_liu` already extracts rmjob as the job ID via `_pqs(_up(link).query).get("rmjob")`,
working around the dedup problem. But `canonical_url` is still used for
URL matching in `inject_job._url_exists()`, which can cause false positives.

**Fix**: filter only tracking params and keep business params.

```python
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "tracking", "source", "mc_cid", "mc_eid",
}

def canonical_url(url: str) -> str:
    from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse
    parsed = urlparse(url.lower().strip())
    filtered_qs = urlencode([
        (k, v) for k, v in parse_qsl(parsed.query)
        if k not in _TRACKING_PARAMS
    ])
    normalized = parsed._replace(query=filtered_qs, fragment="")
    return urlunparse(normalized).rstrip("/")
```

**Scope**: `utils.py` (definition), `inject_job._url_exists()` (usage).
**Note**: after the change, update the assertions in `test_sources.py::TestCanonicalUrl`
(the current tests expect the query to be removed entirely).
**Priority**: medium; the current LiU job ID extraction works around the main risk, but inject dedup still has a hidden issue.

---

## Issue 3: `fetch_chalmers` depends on a migrated domain

**Problem**: `_CTH_BASE = "https://web103.reachmee.com/ext/I003/304"` in `sources.py`
now returns 404. The Chalmers ReachMee system has been migrated, so `fetch_chalmers` can no longer fetch any data.

**Current state**: playwright still attempts to load that URL, then silently returns an empty list after a timeout,
producing no error but zero data, which is hard to notice.

**Investigation direction**: the Chalmers official page `chalmers.se/en/about-chalmers/work-with-us/vacancies/`
embeds ReachMee via an iframe or JS widget, so playwright is needed to confirm the new load path.

```python
# Temporary fix: explicitly record the broken state
def fetch_chalmers(include, exclude=[]):
    print("  ⚠️  Chalmers data source is broken (web103.reachmee.com has migrated), returning an empty list")
    print("     Please visit https://www.chalmers.se/en/about-chalmers/work-with-us/vacancies/")
    print("     Use playwright to inspect the new page structure, then update sources.py")
    return [], {"fetched": 0, "after_include": 0, "after_exclude": 0}
```

Long-term plan: use playwright to load the Chalmers official vacancies page and extract the job list from the embedded widget.
**Priority**: medium; Academic Positions and Varbi already cover some Chalmers positions, so it is not urgent but does affect coverage.

---

## Issue 4: title + company dedup is too aggressive

**Problem**: in `fetch_jobs.py`, `title_seen` dedups using `(normalized_title, company)` as the key,
which wrongly suppresses:

- The same position title at the same organization but posted by different departments (e.g. both KTH Physics and KTH MC2 posted "PhD Student in Materials")
- Batch rotation programs, where multiple positions share the same title but differ in content

**Fix**: use title + company dedup only as a last-resort fallback, prioritizing the job ID.
For an existing (title, company), only skip when the URL is also exactly identical.

```python
# fetch_jobs.py modify the title_seen logic
title_url_seen = {
    (_normalize_title(j.get("title", "")),
     j.get("company", "").lower().strip(),
     canonical_url(j.get("url", ""))): True
    for j in pool.values()
}

# Match the triple-key on check, not the two-key
title_key = (
    _normalize_title(job.get("title", "")),
    job.get("company", "").lower().strip(),
    canonical_url(job.get("url", "")),
)
if title_key in title_url_seen:
    continue
```

**Priority**: low; current impact is limited, and ID dedup is the primary mechanism.

---

## Issue 5: CI lacks automated tests

**Problem**: the CI yaml is already written in `testing.md` but has not yet been added to `.github/workflows/`.
Currently all tests can only be run manually, with no automatic verification on PRs.

**Fix**: create `.github/workflows/test.yml`.

```yaml
name: Unit Tests

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: pip install pytest pytest-mock anthropic httpx beautifulsoup4 streamlit
      - name: Run unit tests
        run: pytest tests/ -v --tb=short
        env:
          ANTHROPIC_API_KEY: test-key
```

**Priority**: high; very low cost, prevents regressions.

---

## Supplementary Testing Plan

The current test coverage is fairly complete; the following are the remaining gaps:

### High-value supplementary tests

**`test_sources.py` — new regression tests after the `canonical_url` change**

After fixing Issue 2, update in sync:

```python
def test_preserves_business_params(self):
    """Business params such as rmjob should be preserved"""
    url = "https://liu.se/vacancies/?rmjob=14533&rmlang=UK"
    result = canonical_url(url)
    assert "rmjob=14533" in result

def test_strips_tracking_params(self):
    url = "https://liu.se/vacancies/?rmjob=14533&utm_source=email"
    result = canonical_url(url)
    assert "utm_source" not in result
    assert "rmjob=14533" in result
```

**`test_fetch_jobs.py` — title+company+url triple-key dedup**

Add after fixing Issue 4:

```python
def test_same_title_company_different_url_both_kept(self, ...):
    """Positions with the same org/title but different URLs should both be kept"""
    ...
```

### Deferred tests (low cost-benefit ratio)

| Module | Reason |
|---|---|
| `review_gui.py` | Streamlit testing infrastructure is complex; the business logic is already tested in modules like `sync_index` |
| `sources.py` network functions | Requires mocking httpx + playwright, large effort; live testing is more effective |

### Live smoke tests (manual, run on demand)

```bash
# Verify each data source independently; no API key needed
pytest tests/test_sources_live.py -v -m live
```

In `tests/test_sources_live.py`, mark with `@pytest.mark.live`; CI does not run it automatically.

---

## Execution Priority

| Issue | Priority | Effort | Benefit |
|---|---|---|---|
| Issue 5: add CI | High | 15 min | Prevents regressions |
| Issue 1: lazy client init | High | 30 min | Test reliability, script usability |
| Issue 3: mark Chalmers broken | Medium | 10 min | Eliminates silent failures |
| Issue 2: canonical_url fix | Medium | 1 hour (incl. updating tests) | LiU inject dedup correctness |
| Issue 4: triple-key dedup | Low | 1 hour (incl. updating tests) | Fewer missed positions |

---

## Things Not Being Done

**Replacing CSV with SQLite**: the current data volume (a few hundred records) is well within CSV's capacity;
introducing SQLite would add maintenance complexity with little clear benefit. Reassess once the data volume exceeds 5000 records.

**`match_jobs` unit test**: requires mocking the Claude API, which is a fair amount of effort,
and the evaluate_jobs integration test already covers the main path, so this is low priority.
