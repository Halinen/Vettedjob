# Job Search Toolkit

**🌐 Language / 语言:** **[English](#english)** · **[中文](#中文)**

A deployable, fork-and-run toolkit that fetches job postings, runs every posting
through a **4-layer legitimacy filter**, and emails you **only the jobs that pass
verification** — so scams, ghost jobs, and fake-remote listings never reach your inbox.

一个可部署、fork 即用的工具箱：抓取岗位 → 经过**四层合法性筛选** → **只把验证通过的岗位**邮件推送给你，让诈骗岗、幽灵岗、虚假远程岗永远进不了你的收件箱。

---

<a name="english"></a>

## English

> Fork this repo, edit `config.json` + `searches/*.json` + your `.env`, and you have
> your own pipeline. Nothing personal is hardcoded.

```text
fetch jobs  →  4-layer legitimacy filter  →  push only verified  →  email
```

### The 4-layer legitimacy filter

This is the core. It answers **"is this job real, legal, and safe to apply to?"** — a
different question from "does this job fit me?" (the optional fit-scorer). A posting
must clear this filter to be pushed.

| Layer | What it does | Why it's there |
| --- | --- | --- |
| **1 — Rule checks** | Upfront fees, salary anomaly, personal/free email, posting age, scam language. | Fast & free, no API cost. A hard red flag here **caps the score** and short-circuits the paid layers. |
| **2 — External verification** | Claude web search: company official site? Glassdoor / review history? Real recruiter? Role on the company's own careers page? | Every finding **quotes its source** (a URL or exact text). |
| **3 — LLM judge** | Scores legitimacy, real-remote honesty, and ghost-job risk, with evidence. | **Every flag must quote the source** — fully explainable. |
| **4 — Aggregate & threshold** | Rules **hard-veto**, the LLM **soft-scores**; combined into a verdict. | Output: a 0–10 score + red/green flags + `pass` / `review` / `reject`. |

Implemented in [scripts/legitimacy.py](scripts/legitimacy.py); the judge prompt is
[data/prompts/legitimacy_judge.md](data/prompts/legitimacy_judge.md). Each layer is a
standalone function and independently testable.

**Verdict logic** — a Layer-1 hard red flag (upfront fee, salary anomaly) or a
"company not found" from Layer 2 caps the score at 3 and forces `reject`. Otherwise the
LLM legitimacy score decides: `>= pass_threshold` → **pass**, `< reject_threshold` →
**reject**, in between → **review**. Only verdicts in `legitimacy.push_verdicts`
(default `["pass"]`) are emailed.

### Architecture

```text
[ Cloud — GitHub Actions, daily ]

  searches/*.json (your search definitions)
        |
  fetch_jobs.py      scan searches, call sources, dedupe -> data/pool.json (30-day TTL)
        |
  evaluate_jobs.py
    ├─ (optional) fit-scoring: Claude scores resume match  -> data/prompts/job_system.md
    ├─ legitimacy filter: 4 layers on every candidate        -> scripts/legitimacy.py
    └─ push only PASS jobs -> append data/eval_log.csv + send one email
        |
  git commit & push (pool.json / eval_log.csv / last_run.json)

[ Local — optional, for application tracking ]

  sync_index.py    eval_log -> jobs_index.csv (adds your decision columns)
  review_gui.py    Streamlit GUI: review, track applications, build per-job workspaces
```

Both gates run: with `fit_scoring.enabled = true`, a job must pass fit-scoring **and**
legitimacy. Set `fit_scoring.enabled = false` to push purely on legitimacy.

### Quick start

```bash
pip install -r requirements.txt
cp .env.example .env
# edit .env: ANTHROPIC_API_KEY / EMAIL_FROM / EMAIL_TO / EMAIL_APP_PASSWORD
```

1. **Configure** — edit [config.json](config.json): model, `jobspy` region, legitimacy
   thresholds, whether fit-scoring is on.
2. **Define searches** — copy an example in [searches/](searches/) and edit its
   `include` / `exclude` / `sources`.
3. **(Optional) add your resume** — fit-scoring reads
   [data/profiles/resume_eval.md](data/profiles/resume_eval.md). Skip it if you only
   want legitimacy filtering.
4. **Run locally** to test: `python scripts/cloud_run.py`
5. **Deploy** — add the secrets below and let
   [.github/workflows/daily_jobs.yml](.github/workflows/daily_jobs.yml) run it daily.

### Web app

The repo also includes a lightweight FastAPI web app for interactive checks:

```bash
pip install -r requirements.txt
uvicorn web.app:app --reload --port 8000
```

Open <http://localhost:8000>. The web app supports:

- **Check**: paste one job posting and run the explainable legitimacy filter.
- **Fetch**: run a small `jobspy` search, then assess the returned jobs.
- **Resume**: upload or paste a temporary resume for fit scoring during fetch.

Docker:

```bash
docker build -t vettedjob .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=sk-ant-... vettedjob
```

Temporary resume uploads are written to `web/_tmp_resumes/` and are ignored by git.

**GitHub Actions secrets** (Settings → Secrets → Actions):

| Secret | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API key (`sk-ant-...`) |
| `EMAIL_FROM` | Gmail address (sender) |
| `EMAIL_TO` | Where to receive results |
| `EMAIL_APP_PASSWORD` | Gmail App Password (16 chars, requires 2FA) |

### Configuration (`config.json`)

```jsonc
{
  "model": "claude-sonnet-4-6",
  "remote_only": false,           // true = only remote jobs survive (see below)
  "jobspy": { "location": "", "country": "", "hours_old": 168 },
  "legitimacy": {
    "enabled": true,
    "fast_fail": true,            // skip paid layers after a Layer-1 hard red flag
    "stale_posting_days": 60,     // older postings get a ghost-job yellow flag
    "pass_threshold": 7.0,        // score >= this -> pass
    "reject_threshold": 4.0,      // score < this  -> reject
    "push_verdicts": ["pass"],    // which verdicts get emailed
    "layer2": { "enabled": true, "max_searches": 4 },
    "layer3": { "prompt": "data/prompts/legitimacy_judge.md" }
  },
  "fit_scoring": { "enabled": true, "min_score": 6 }
}
```

- **Remote only**: set `remote_only: true`. This (1) makes the jobspy source fetch
  remote-only, (2) drops any non-remote posting from *every* source at fetch time, and
  (3) lets the legitimacy filter hard-veto a posting that turns out not to be a genuine
  remote role. A source-provided remote flag is trusted; otherwise the posting text is
  scanned (and on-site / hybrid / relocation wording disqualifies it).
- **Legitimacy-only** (push any verified real job regardless of fit): set
  `fit_scoring.enabled = false`.
- **Also surface ambiguous jobs**: set `push_verdicts: ["pass", "review"]`.
- **`jobspy` region**: `location`/`country` are passed to the Indeed + Google Jobs
  source. Leave blank for a worldwide search.

### Adding a data source

`searches/*.json` drives everything; no code change needed to add keywords or reuse a
source. The most portable source is `jobspy` (Indeed + Google Jobs), whose region comes
from `config.json`. To add a new site, implement a function in
[scripts/sources.py](scripts/sources.py) and register it:

```python
def fetch_mysite(include: list[str], exclude: list[str] = [],
                 max_results: int = 20) -> tuple[list[dict], dict]:
    ...
    # return (jobs, {"fetched": n, "after_include": n, "after_exclude": n})

SOURCE_REGISTRY["mysite"] = fetch_mysite
```

Each returned job dict needs: `id` (globally unique), `title`, `company`, `description`
(≤1000 chars), `url`.

> The repo ships several real working sources (jobspy, EURAXESS, plus a few Swedish
> university scrapers) as examples. Keep, edit, or delete them as you like.

### Output columns

`data/eval_log.csv` is append-only and immutable. Beyond the fit-scoring columns it
carries the legitimacy verdict:

| Column | Meaning |
| --- | --- |
| `legit_verdict` | `pass` / `review` / `reject` |
| `legit_score` | 0–10 legitimacy score |
| `legit_red_flags` | red flags, each with its quoted source |
| `legit_green_flags` | positive verification signals |

### Tests

```bash
pytest tests/                       # all
pytest tests/test_legitimacy.py     # the 4-layer filter (offline, no API)
```

The legitimacy tests mock the paid layers, so they run with no API key.

### Cost

| Service | Cost |
| --- | --- |
| GitHub Actions | Free tier (~5–10 min/day) |
| Claude API | Layer 1 is free; Layers 2–3 run only on candidates. `fast_fail` skips paid layers on obvious scams. |
| Gmail | Free |

---

<a name="中文"></a>

## 中文

> Fork 本仓库，编辑 `config.json` + `searches/*.json` + 你的 `.env`，就拥有了属于你自己的流程。代码里没有任何写死的个人信息。

```text
抓取岗位  →  四层合法性筛选  →  只推送验证通过的  →  邮件
```

### 四层合法性筛选

这是项目的核心。它回答的是**「这个岗位是不是真的、合法的、可以安全投递」**——和「这个岗位适不适合我」（可选的匹配打分层）是两个不同的问题。一个岗位必须通过这一层筛选才会被推送。

| 层 | 做什么 | 为什么 |
| --- | --- | --- |
| **1 — 规则检查** | 预收费用、薪资异常、个人/免费邮箱、岗位发布时长、诈骗话术。 | 快且免费，不耗 API。这里出现硬红旗会**封顶分数**并跳过后续付费层。 |
| **2 — 外部验证** | Claude 联网搜索：公司有没有官网？有没有 Glassdoor / 评价历史？招聘人是否真实？该岗位是否出现在公司自己的招聘页？ | 每条结论都必须**引用来源**（URL 或原文）。 |
| **3 — LLM 评审** | 给合法性、真实远程程度、幽灵岗风险打分，并附证据。 | **每个 flag 都必须引用来源**——完全可解释。 |
| **4 — 聚合与阈值** | 规则**硬否决**，LLM **软打分**；合成最终判定。 | 输出：0–10 分 + 红/绿旗 + `pass` / `review` / `reject`。 |

实现见 [scripts/legitimacy.py](scripts/legitimacy.py)；评审 prompt 见
[data/prompts/legitimacy_judge.md](data/prompts/legitimacy_judge.md)。每一层都是独立函数，可单独测试。

**判定逻辑**——第 1 层的硬红旗（预收费、薪资异常）或第 2 层的「公司查无此号」会把分数封顶到 3 并强制 `reject`。否则由 LLM 合法性分数决定：`>= pass_threshold` → **pass**，`< reject_threshold` → **reject**，中间 → **review**。只有在 `legitimacy.push_verdicts`（默认 `["pass"]`）里的判定才会发邮件。

### 架构

```text
【云端 — GitHub Actions，每日】

  searches/*.json（你的搜索定义）
        |
  fetch_jobs.py      扫描 searches，调用数据源，去重 -> data/pool.json（30 天 TTL）
        |
  evaluate_jobs.py
    ├─（可选）匹配打分：Claude 评估简历契合度  -> data/prompts/job_system.md
    ├─ 合法性筛选：对每个候选岗位跑四层          -> scripts/legitimacy.py
    └─ 只推送 PASS 的岗位 -> 追加 data/eval_log.csv + 发一封邮件
        |
  git commit & push（pool.json / eval_log.csv / last_run.json）

【本地 — 可选，用于投递追踪】

  sync_index.py    eval_log -> jobs_index.csv（加上你的决策列）
  review_gui.py    Streamlit GUI：审核、追踪投递、为单个岗位建工作区
```

两道门都生效：当 `fit_scoring.enabled = true` 时，岗位必须**同时**通过匹配打分**和**合法性筛选。把 `fit_scoring.enabled = false` 则纯按合法性推送。

### 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env：ANTHROPIC_API_KEY / EMAIL_FROM / EMAIL_TO / EMAIL_APP_PASSWORD
```

1. **配置** — 编辑 [config.json](config.json)：模型、`jobspy` 地区、合法性阈值、是否开启匹配打分。
2. **定义搜索** — 复制 [searches/](searches/) 里的示例，改它的 `include` / `exclude` / `sources`。
3. **（可选）放入你的简历** — 匹配打分会读
   [data/profiles/resume_eval.md](data/profiles/resume_eval.md)。只要合法性筛选的话可以跳过。
4. **本地试跑**：`python scripts/cloud_run.py`
5. **部署** — 配好下面的 secrets，让
   [.github/workflows/daily_jobs.yml](.github/workflows/daily_jobs.yml) 每天自动运行。

**GitHub Actions Secrets**（Settings → Secrets → Actions）：

| Secret | 值 |
| --- | --- |
| `ANTHROPIC_API_KEY` | Anthropic API Key（`sk-ant-...`） |
| `EMAIL_FROM` | Gmail 地址（发件） |
| `EMAIL_TO` | 接收结果的地址 |
| `EMAIL_APP_PASSWORD` | Gmail 应用专用密码（16 位，需开启两步验证） |

### 配置（`config.json`）

```jsonc
{
  "model": "claude-sonnet-4-6",
  "remote_only": false,           // true = 只保留远程岗位（见下）
  "jobspy": { "location": "", "country": "", "hours_old": 168 },
  "legitimacy": {
    "enabled": true,
    "fast_fail": true,            // 第 1 层出硬红旗后跳过付费层
    "stale_posting_days": 60,     // 更久的岗位会得到幽灵岗黄旗
    "pass_threshold": 7.0,        // 分数 >= 此值 -> pass
    "reject_threshold": 4.0,      // 分数 < 此值  -> reject
    "push_verdicts": ["pass"],    // 哪些判定会发邮件
    "layer2": { "enabled": true, "max_searches": 4 },
    "layer3": { "prompt": "data/prompts/legitimacy_judge.md" }
  },
  "fit_scoring": { "enabled": true, "min_score": 6 }
}
```

- **只找远程岗位**：设 `remote_only: true`。这会（1）让 jobspy 数据源只抓远程岗，（2）在抓取阶段把**所有**数据源里的非远程岗位剔除，（3）让合法性筛选对「号称远程但其实不是」的岗位硬否决。若数据源自带远程标记则采信它，否则扫描岗位文本（出现 on-site / hybrid / 需搬迁等措辞即判为非远程）。
- **只做合法性筛选**（凡是验证为真的岗位都推送，不看契合度）：设 `fit_scoring.enabled = false`。
- **也想看到模糊岗位**：设 `push_verdicts: ["pass", "review"]`。
- **`jobspy` 地区**：`location`/`country` 会传给 Indeed + Google Jobs 数据源。留空则全球搜索。

### 新增数据源

`searches/*.json` 控制一切；改关键词或复用数据源无需改代码。最通用的数据源是 `jobspy`（Indeed + Google Jobs），其地区来自 `config.json`。要接入新站点，在
[scripts/sources.py](scripts/sources.py) 写一个函数并注册：

```python
def fetch_mysite(include: list[str], exclude: list[str] = [],
                 max_results: int = 20) -> tuple[list[dict], dict]:
    ...
    # 返回 (jobs, {"fetched": n, "after_include": n, "after_exclude": n})

SOURCE_REGISTRY["mysite"] = fetch_mysite
```

每个返回的 job dict 必须含：`id`（全局唯一）、`title`、`company`、`description`（≤1000 字符）、`url`。

> 仓库自带几个能用的真实数据源（jobspy、EURAXESS，以及几个瑞典高校爬虫）作为示例。可以保留、修改或删除。

### 输出列

`data/eval_log.csv` 只追加、不可变。除匹配打分列外，新增了合法性判定列：

| 列 | 含义 |
| --- | --- |
| `legit_verdict` | `pass` / `review` / `reject` |
| `legit_score` | 0–10 合法性分数 |
| `legit_red_flags` | 红旗，每条附引用来源 |
| `legit_green_flags` | 正向验证信号 |

### 测试

```bash
pytest tests/                       # 全部
pytest tests/test_legitimacy.py     # 四层筛选（离线，无需 API）
```

合法性测试 mock 掉了付费层，所以无需 API key 即可运行。

### 费用

| 服务 | 费用 |
| --- | --- |
| GitHub Actions | 免费额度（约 5–10 分钟/天） |
| Claude API | 第 1 层免费；第 2–3 层只对候选岗位跑。`fast_fail` 会在明显诈骗岗上跳过付费层。 |
| Gmail | 免费 |
