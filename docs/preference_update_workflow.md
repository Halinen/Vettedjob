<!-- NOTE: may reference legacy behavior -->
# Preference Analysis and Prompt Update Workflow

> This document is meant to be read by Claude Code. It describes how to periodically analyze application data, distill user preferences,
> update the profile and prompts, and maintain a change log.
>
> **How to invoke**: open the project in Claude Code and say:
> **"Please run the preference analysis workflow"**

---

## 1. Workflow Overview

### When to run

It is recommended to run in the following situations:
- 10+ new records with `review_decision != ""` have accumulated
- You notice a clear scoring bias (Claude scores high but you ignore it, or vice versa)
- You proactively want to adjust the preference in some direction

### What it does

```
Read change log → determine analysis scope
       ↓
Read jobs_index.csv (new review records)
       ↓
Cross-analysis: Claude score vs user decision vs user reasoning
       ↓
Distill preference rules (acceptance patterns + exclusion patterns)
       ↓
Compare against existing active_rules, flag conflicts
       ↓
Present suggestions to the user, confirm one by one
       ↓
Apply confirmed changes → update change log
```

---

## 2. Files Involved

| File | Role | Read/Write |
|---|---|---|
| `jobs_index.csv` | Source of analysis data | Read-only |
| `data/eval_log.csv` | Supplements Claude's detailed reason/research_fit | Read-only |
| `data/prompt_update_log.json` | Change log + active_rules | Read/Write |
| `data/exclusions.json` | Title-keyword exclusion list (filtered before Claude evaluation) | Read/Write |
| `data/profiles/*.md` | User profile, modifiable | Read/Write |
| `prompts/*.md` | Evaluation prompts, modifiable | Read/Write |

---

## 3. Data Structures

### `data/prompt_update_log.json`

This is the state hub for the entire workflow, created automatically on first run.

```json
{
  "last_analyzed_date": "2026-04-10",
  "last_analyzed_reviewed_count": 23,
  "active_rules": [
    {
      "id": "rule_phd_001",
      "added_date": "2026-04-12",
      "direction": "PhD positions",
      "type": "exclusion",
      "description": "Process metallurgy (blast furnace/converter/ironmaking) topics do not fit the research background and should be scored below 4",
      "evidence": ["af_30868073 (rejected: industrial ironmaking direction)", "af_30868045 (rejected: same as above)"],
      "applied_to": "prompts/phd_search.md",
      "status": "active"
    },
    {
      "id": "rule_phd_002",
      "added_date": "2026-04-12",
      "direction": "PhD positions",
      "type": "preference",
      "description": "The combination of plasma-assisted thin films/coatings + top-tier Swedish universities (KTH/Uppsala/Chalmers) should be prioritized with a high score",
      "evidence": ["af_30811712 (accepted: score 9, Chalmers)", "varbi_921107 (accepted: score 8, KTH)"],
      "applied_to": "data/profiles/phd.md",
      "status": "active"
    }
  ],
  "sessions": [
    {
      "date": "2026-04-12",
      "analyzed_date_range": "2026-04-06 ~ 2026-04-10",
      "new_reviewed_count": 23,
      "proposed": 4,
      "accepted": 3,
      "rejected": 1,
      "files_modified": ["data/profiles/phd.md", "prompts/phd_search.md"],
      "summary": "Added process metallurgy exclusion rule; strengthened plasma + top-university weighting; supplemented ERDA characterization details in the phd profile"
    }
  ]
}
```

**Field descriptions:**

| Field | Description |
|---|---|
| `last_analyzed_date` | The latest evaluated_at date covered by the previous analysis |
| `last_analyzed_reviewed_count` | Total reviewed entries at the previous analysis, used to detect whether there is new data |
| `active_rules` | List of currently effective preference rules |
| `rule.id` | Unique identifier, format `rule_{direction abbreviation}_{number}` |
| `rule.type` | `exclusion` / `preference` (positive preference) / `calibration` (score calibration) / `profile_note` (profile supplement) |
| `rule.applied_to` | Which file the rule affects |
| `rule.status` | `active` / `superseded` (overridden by a new rule, history retained) |
| `sessions` | Summary of each run, append-only, never modified |

---

### Fields used from `jobs_index.csv` during analysis

| Field | Use |
|---|---|
| `id` | Primary key matching eval_log, used to cross-query reason/research_fit |
| `evaluated_at` | Determines analysis scope (> last_analyzed_date) |
| `direction` | Analyze separately by direction |
| `title` / `company` | Identify the position type |
| `score` / `score_source` | Claude score vs manual score |
| `review_decision` | `accepted` / `rejected`; skip if empty |
| `review_reason` | The user's reasoning for the decision, **the most important input** |
| `matched` | Whether it passed Claude's score threshold |

---

## 4. Analysis Protocol (steps for Claude Code to execute)

### Step 1: Read state, determine scope

1. Read `data/prompt_update_log.json` and get `last_analyzed_date`
2. Read `jobs_index.csv` and filter for:
   - `review_decision != ""` (reviewed)
   - `evaluated_at > last_analyzed_date` (new data)
3. If there are 0 new records: tell the user "no new review data" and exit
4. Tell the user the scope of this analysis: `X new reviews, covering {date range}`

### Step 2: Data extraction

Group by direction and build an analysis table for each direction:

```
Direction: PhD positions
┌────────────────────┬───────┬───────────────┬──────────────────────────┐
│ title              │ score │ decision      │ review_reason            │
├────────────────────┼───────┼───────────────┼──────────────────────────┤
│ PhD Plasma KTH     │  8    │ accepted      │ plasma direction is a perfect fit │
│ Process Metallurgy │  7    │ rejected      │ industrial ironmaking, wrong direction │
│ Polymer Chemistry  │  7    │ rejected      │ organic chemistry, unrelated to background │
└────────────────────┴───────┴───────────────┴──────────────────────────┘
```

At the same time, supplement the corresponding rows' `reason` and `research_fit` fields from `eval_log.csv`.

### Step 3: Pattern recognition

For each direction, look for the following patterns:

**Calibration bias** (high priority)
- Claude scored ≥7 but the user rejected: what did Claude overestimate?
- Claude scored <7 (matched=false) but the user explicitly wants to see it: what did Claude miss?

**Acceptance patterns**
- Common characteristics of accepted positions (keywords, organization, topic direction)
- Compare with the existing profile description: are there preferences that are not adequately expressed?

**Exclusion patterns**
- Common characteristics of rejected positions (especially high-scoring ones that were rejected)
- Are these characteristics already covered by an exclusion rule in the prompt?

### Step 3.5: Keyword exclusion opportunity identification

Before generating prompt modification suggestions, additionally check whether an exclusion pattern is better implemented via `data/exclusions.json` (more thorough than modifying the prompt, since it filters before the Claude call):

**Scenarios suited to keyword exclusion** (the match appears in the job title):

- Position-type words: `postdoktor`, `postdoc`, `sommarjobb`, etc.
- Field words: `metallurgy`, `polymer chemistry`, and other highly specific directions

**Scenarios not suited to keyword exclusion** (the condition is in the body, not the title):

- "Requires a PhD", "chemical engineering degree", and other qualification constraints → apply a prompt low-score rule
- "Pure computation/modeling direction" → the title may not contain a modeling word, so it must be identified via the prompt

**`data/exclusions.json` structure**:

```json
{
  "global": ["postdoc", "post-doc", "post doc", "postdoctoral", "postdoktor"],
  "per_direction": {
    "PhD positions": [],
    "RF/microwave engineering": [],
    "Industrial/NPI": [],
    "Materials research": ["metallurgy"]
  }
}
```

- `global`: applies to all directions, matches the job **title** (case-insensitive substring)
- `per_direction`: applies only to the specified direction
- On a match, write to eval_log (`reason: "excluded: <keyword>"`) without consuming Claude tokens

If there is a pattern suited to keyword exclusion, list it separately as a suggestion with `type: "keyword_exclusion"` among the change suggestions.

### Step 4: Generate change suggestions

The format of each suggestion:

```
[Suggestion #1]
Type: exclusion
Direction: PhD positions
Target file: prompts/phd_search.md
Basis: the following 3 positions were rejected, and review_reason all mentions a wrong direction:
  - af_30868073: Process Metallurgy @ LTU ("industrial ironmaking direction")
  - af_30868045: Process Metallurgy @ LTU (same as above)
  - varbi_XXXX: Steel Manufacturing PhD ("unrelated to my plasma background")

There is no corresponding exclusion rule in the current prompt.

Suggest appending to the end of prompts/phd_search.md:
---
## Low-score rules
For the following topic types, even with keyword matches, score no higher than 4:
- Process metallurgy / steelmaking / blast-furnace ironmaking types
- Pure organic chemistry / polymer synthesis (without materials-physics characterization)

[Accept? (y/n/modify)]
```

### Step 5: Conflict detection

Before presenting each suggestion, check `active_rules`:

**Conflict definition**: a new suggestion's `description` is opposite to or contradicts some `active_rule.description` in direction and topic.

**Conflict handling**:

```
⚠️ Conflict detected

New suggestion: lower the weight of "thin film deposition" positions
Existing rule rule_phd_002 (2026-04-12): plasma-assisted thin films/coatings prioritized with a high score

These two rules contradict each other. Please decide:
  1. Keep the old rule, discard the new suggestion
  2. Replace the old rule with the new one (mark the old rule as superseded)
  3. Manually explain how to reconcile the two
```

### Step 6: Confirm one by one

Present suggestions one at a time and wait for an explicit user response (y / n / modified content) before processing the next.
**Do not batch process** and do not assume the user's intent.

### Step 7: Apply changes + update log

For each confirmed suggestion:
1. Directly modify the target file (profile or prompt)
2. Append the new rule to `active_rules` (or mark the old rule as `superseded`)
3. Record this session's summary in the `sessions` array

Finally update:
```json
"last_analyzed_date": "<latest evaluated_at of this run>",
"last_analyzed_reviewed_count": <current total reviewed count>
```

---

## 5. Scope of Modifiable Content

### `data/profiles/*.md`

Modifiable parts:
- The `## Research directions of interest for PhD` list (reorder, add/remove entries)
- The `## Skills` list (add new skills)
- Append a `## Additional preferences` section at the end (do not alter existing content)

**Not allowed to modify**: education, work experience, personal basic information (these are objective facts)

### `prompts/*.md`

Modifiable parts:
- Append scoring rules at the end (`## Low-score rules` / `## High-score preferences`)
- Modify the `score >= X` threshold value
- Adjust the `at most N` count limit

**Not allowed to modify**: the `{profile}` and `{jobs}` placeholders, the JSON output format definition

---

## 6. First-Run Initialization

If `data/prompt_update_log.json` does not exist, automatically create the initial structure:

```json
{
  "last_analyzed_date": "2000-01-01",
  "last_analyzed_reviewed_count": 0,
  "active_rules": [],
  "sessions": []
}
```

The first run analyzes all existing records that have a review_decision.

---

## 7. Notes

- **Do not run automatically**: always wait for the user to explicitly invoke it; do not auto-trigger it in local_run.py
- **Confirm each suggestion independently**: even if the user keeps saying y, execute one by one to avoid mistakes
- **Show a diff before modifying**: for profile/prompt changes, first show "before vs after", then write the file after confirmation
- **sessions are append-only**: history is never overwritten, making it easy to trace back
- **Rules are not deleted**: old rules are marked `superseded` rather than deleted, preserving decision history
