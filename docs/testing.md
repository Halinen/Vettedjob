# Testing Plan

## Overview

This project's tests are organized in three layers, following the principle: **all tests that do not depend on the network run automatically locally/in CI; those that depend on external services run only during manual smoke tests.**

```
tests/
  test_sources.py       Pure functions: _match_any, canonical_url, _stable_id, _varbi_id
  test_fetch_jobs.py    Pure functions + inject_queue logic; Bug 1 regression test
  test_sync_index.py    sync append/rebuild modes; temp-directory isolation
  test_inject_job.py    Queue CRUD: dedup, manual_score, clear
  test_workspace.py     Workspace file-tree structure and contents
  test_evaluate_jobs.py Integration: mock match_jobs + send_email; Bug 2 regression test
  test_utils.py         build_seen_ids, canonical_url
```

---

## Fixed Bugs

### Bug 1 — `fetch_jobs.py` empty-list `include` override ineffective

**File**: [scripts/fetch_jobs.py](../scripts/fetch_jobs.py), originally lines 83-84

**Problem**:

```python
# Before the fix (buggy)
inc = src_cfg.get("include") or search.get("include", [])
```

When a source config explicitly sets `"include": []` (intent: no filtering, keep everything),
Python's `[] or ...` falls through to the search-level include, causing the "no filtering" intent to be ignored.

**Fix**:

```python
# After the fix
inc = src_cfg["include"] if "include" in src_cfg else search.get("include", [])
```

**Regression test**: `tests/test_fetch_jobs.py::TestIncludeExcludeOverride`

---

### Bug 2 — `evaluate_jobs.py` exact URL match causes missed `matched` flag

**File**: [scripts/evaluate_jobs.py](../scripts/evaluate_jobs.py), originally line 133

**Problem**:

```python
# Before the fix (buggy)
claude_result = next(
    (m for m in matched if m.get("url") == job.get("url")), None
)
```

The URL returned by the Claude API may differ subtly from the URL stored in the pool (e.g. a trailing slash),
resulting in `matched=False` and an empty `score` column, while `evaluated=True` is already set,
so the job is never re-evaluated.

**Fix**:

```python
# After the fix (compare after canonical_url normalization)
job_canon = canonical_url(job.get("url", ""))
claude_result = next(
    (m for m in matched if canonical_url(m.get("url", "")) == job_canon), None
)
```

The `canonical_url` import was also added to the import lines.

**Regression test**: `tests/test_evaluate_jobs.py::TestUrlNormalizationRegression`

---

## Running Tests

### Install dependencies

```bash
pip install pytest pytest-mock
```

### Run all unit tests (no network/API key needed)

```bash
pytest tests/ -v
```

### Run a single module

```bash
pytest tests/test_fetch_jobs.py -v
pytest tests/test_evaluate_jobs.py -v
```

### Run only regression tests

```bash
pytest tests/ -v -k "regression or Regression or Bug"
```

---

## Description of Each Test File

### `test_sources.py`

Covers all pure functions in `sources.py`, with no network requests.

| Test class | Coverage |
|---|---|
| `TestCanonicalUrl` | Trailing slash, case, query/fragment cleanup |
| `TestStableId` | Format, determinism, difference across URLs |
| `TestVarbiId` | jobID extraction, fallback when no ID |
| `TestMatchAny` | Case, empty list, empty string, substring matching |
| `TestSourceRegistry` | Registry completeness and callability |

### `test_fetch_jobs.py`

| Test class | Coverage |
|---|---|
| `TestNormalizeTitle` | Case, whitespace folding |
| `TestCleanupPool` | TTL boundaries (30 days/37 days), evaluated/not evaluated, missing fields |
| `TestProcessInjectQueue` | New entries added to pool, seen skipped, processed skipped, manual_score |
| `TestLoadSearches` | Non-`.json` files ignored |
| `TestTitleDedup` | Same org/same title but different URL not deduped (triple-key) |
| `TestIncludeExcludeOverride` | **Bug 1 regression**: empty list does not fall through |

### `test_sync_index.py`

| Test class | Coverage |
|---|---|
| `TestWaitingDays` | Normal date, empty string, invalid date |
| `TestSyncAppend` | Append new rows, idempotency, default status=pending |
| `TestSyncFull` | Rebuild, preserve user decisions, workspace detection, PDF detection |

### `test_inject_job.py`

| Test class | Coverage |
|---|---|
| `TestMakeId` | Format, determinism, URL normalization |
| `TestUrlExists` | In-queue dedup, in-pool dedup, normalized matching |
| `TestInjectOne` | Add, skip duplicate, manual_score, default fields |
| `TestClearQueue` | Clear only unprocessed, keep processed |

### `test_workspace.py`

| Test class | Coverage |
|---|---|
| `TestMakeFolderName` | Date prefix, lowercase, special characters, truncation |
| `TestCreateWorkspace` | Directory tree, required files, status.json contents, template selection, idempotency |

### `test_evaluate_jobs.py`

| Test class | Coverage |
|---|---|
| `TestAppendEvalLog` | Header creation, append without duplicating header, extra fields ignored |
| `TestEvaluateAllManualScore` | Skip Claude, matched=True, pool marked |
| `TestEvaluateAllClaudeScore` | Low score matched=False, Claude failure not marked, success marked |
| `TestUrlNormalizationRegression` | **Bug 2 regression**: trailing-slash URL still matches |

### `test_utils.py`

| Test class | Coverage |
|---|---|
| `TestCanonicalUrl` | trailing slash, case, tracking-parameter filtering, business-parameter preservation |
| `TestBuildSeenIds` | Combined dedup across pool + eval_log, tolerance for missing files, skip empty id |

---

## Known Limitations and Future Testing Directions

### Currently uncovered

| Module | Reason | Suggestion |
|---|---|---|
| `sources.py` network functions (`fetch_varbi`, etc.) | Require real network | Extract the HTTP layer, mock `httpx.get` |
| `review_gui.py` | Streamlit testing is complex | Separate business logic from the UI, then test it independently |
| `utils.py::match_jobs` | Calls the real Claude API | mock `client.messages.create` |
| `utils.py::send_email` | Depends on SMTP | mock `smtplib.SMTP_SSL` |

### End-to-end smoke tests (manual, network required)

Create `tests/test_sources_live.py` to verify that each fetch function actually returns the expected structure:

```python
# Requires the pytest -m live marker; not run automatically in CI
@pytest.mark.live
def test_fetch_varbi_returns_valid_structure():
    jobs, stats = fetch_varbi(include=["phd"])
    assert "fetched" in stats
    for job in jobs:
        assert all(k in job for k in ["id", "title", "company", "url", "description"])
```

### Resolved historical issues

| Issue | Fix location | Status |
| --- | --- | --- |
| `canonical_url` defined in three places | `utils.py` (definition) + `sources.py`/`inject_job.py` (import) | ✅ Fixed |
| `utils.py` module-level client initialization | `utils.py::get_client()` lazy initialization | ✅ Fixed |
| `canonical_url` removed all query params | `_TRACKING_PARAMS` + new implementation, preserves business params | ✅ Fixed |
| Bug 1: include empty-list fall-through | `fetch_jobs.py` `"include" in src_cfg` | ✅ Fixed |
| Bug 2: trailing-slash URL caused missed `matched` flag | `evaluate_jobs.py` canonical_url comparison | ✅ Fixed |
| title+company dedup too aggressive | `fetch_jobs.py` triple-key (adds URL) | ✅ Fixed |
| Chalmers URL pointed to an internal domain requiring a Referer | `sources.py` switched to the official site URL | ✅ Fixed |

---

## CI

`.github/workflows/test.yml` is configured and runs automatically on every push/PR:

```bash
pytest tests/ -v --tb=short
# No real API key needed; all external dependencies are mocked
```
