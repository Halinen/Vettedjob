"""
Job Review GUI — Streamlit (three tabs).
Data source: jobs_index.csv
Run: streamlit run scripts/review_gui.py
"""

import csv
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import streamlit as st

ROOT          = Path(__file__).parent.parent
INDEX_FILE    = ROOT / "jobs_index.csv"
EVAL_LOG      = ROOT / "data" / "eval_log.csv"
POOL_PATH     = ROOT / "data" / "pool.json"
LAST_RUN_PATH = ROOT / "data" / "last_run.json"
JOBS_DIR      = ROOT / "jobs"

st.set_page_config(page_title="Job Review", layout="wide")


# ── jobs_index.csv read/write helpers ─────────────────────
def load_index() -> list[dict]:
    if not INDEX_FILE.exists():
        st.error("jobs_index.csv not found. Run first: python3 scripts/sync_index.py --rebuild")
        st.stop()
    with INDEX_FILE.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_index(rows: list[dict]):
    if not rows:
        return
    fields = list(rows[0].keys())
    with INDEX_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def update_row(job_id: str, updates: dict):
    rows = load_index()
    for row in rows:
        if row["id"] == job_id:
            row.update(updates)
            break
    save_index(rows)


def _source_platform(src: str) -> str:
    """Map a source id (`<search_id>_<source_type>`) to a readable platform name.

    Generic: the platform is the trailing token after the last underscore, which
    matches the SOURCE_REGISTRY key (jobspy, euraxess, varbi, ...). Falls back to
    the raw id when there is no underscore.
    """
    if src == "manual":
        return "Manual"
    if "_" in src:
        return src.rsplit("_", 1)[-1].title()
    return src or "?"


# ── Tab 1: Overview ───────────────────────────────────────
def tab_overview():
    st.header("Overview")

    import pandas as pd

    # URL-join warnings from the last cloud run
    if LAST_RUN_PATH.exists():
        last_run = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
        join_warnings = last_run.get("url_join_warnings", [])
        if join_warnings:
            st.warning(f"Last run had {len(join_warnings)} URL-join failures; these "
                       f"postings may be mis-marked as unmatched:\n\n"
                       + "\n".join(f"- {w}" for w in join_warnings))

    rows = load_index()
    total_index = len(rows)
    matched     = sum(1 for r in rows if str(r.get("matched", "")).lower() == "true")
    passed      = sum(1 for r in rows if r.get("legit_verdict") == "pass")
    rejected    = sum(1 for r in rows if r.get("legit_verdict") == "reject")
    applied     = sum(1 for r in rows if r.get("status") in ("applied", "interview", "offer"))
    unreviewed  = sum(1 for r in rows
                      if str(r.get("matched", "")).lower() == "true"
                      and not r.get("review_decision"))

    # pool pending
    pool_pending = 0
    pool_data: dict = {}
    if POOL_PATH.exists():
        pool_data = json.loads(POOL_PATH.read_text(encoding="utf-8"))
        pool_pending = sum(1 for j in pool_data.values() if not j.get("evaluated"))

    # funnel
    st.subheader("Funnel")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Pool pending", pool_pending)
    c2.metric("Evaluated", total_index)
    c3.metric("Fit-matched", matched)
    c4.metric("Legit pass", passed)
    c5.metric("Applied", applied)

    st.divider()

    # legitimacy verdict breakdown
    st.subheader("Legitimacy verdicts")
    review_cnt = sum(1 for r in rows if r.get("legit_verdict") == "review")
    v1, v2, v3 = st.columns(3)
    v1.metric("PASS", passed)
    v2.metric("REVIEW", review_cnt)
    v3.metric("REJECT", rejected)

    st.divider()

    # fit-matched distribution by type
    st.subheader("Fit-matched by type")
    directions: dict[str, int] = {}
    for r in rows:
        if str(r.get("matched", "")).lower() == "true":
            d = r.get("direction", "other") or "other"
            directions[d] = directions.get(d, 0) + 1
    if directions:
        df = pd.DataFrame({"type": list(directions.keys()),
                           "matched": list(directions.values())})
        st.bar_chart(df.set_index("type"))

    st.divider()

    # source coverage
    st.subheader("Source coverage")

    query_stats: dict[str, dict] = {}

    def _qs(src: str) -> dict:
        if src not in query_stats:
            query_stats[src] = {"pool": 0, "evaluated": 0, "matched": 0, "accepted": 0}
        return query_stats[src]

    for j in pool_data.values():
        if not j.get("evaluated"):
            _qs(j.get("source", "?"))["pool"] += 1

    for r in rows:
        src = r.get("source", "?")
        _qs(src)["evaluated"] += 1
        if str(r.get("matched", "")).lower() == "true":
            _qs(src)["matched"] += 1
        if r.get("review_decision") == "accepted":
            _qs(src)["accepted"] += 1

    # platform-level rollup (derived from the source ids present in the data)
    platform_stats: dict[str, dict] = {}
    for src, s in query_stats.items():
        plat = _source_platform(src)
        if plat not in platform_stats:
            platform_stats[plat] = {"pool": 0, "evaluated": 0, "matched": 0, "accepted": 0}
        for k in ("pool", "evaluated", "matched", "accepted"):
            platform_stats[plat][k] += s[k]

    if platform_stats:
        plat_df = pd.DataFrame([
            {"platform": p, "pool pending": s["pool"], "evaluated": s["evaluated"],
             "matched": s["matched"], "accepted": s["accepted"]}
            for p, s in platform_stats.items()
        ]).set_index("platform")
        st.dataframe(plat_df, use_container_width=True)

    # per-source detail (collapsible)
    with st.expander("Per-source detail"):
        last_run_stats: dict = {}
        run_date = ""
        if LAST_RUN_PATH.exists():
            last_run = json.loads(LAST_RUN_PATH.read_text(encoding="utf-8"))
            run_date = last_run.get("run_at", "")
            last_run_stats = last_run.get("searches", {})

        if run_date:
            st.caption(f"Last run: {run_date}")

        rows_query = []
        for q, s in sorted(query_stats.items()):
            row = {
                "source id": q,
                "platform": _source_platform(q),
                "pool pending": s["pool"],
                "evaluated": s["evaluated"],
                "matched": s["matched"],
                "accepted": s["accepted"],
            }
            if q in last_run_stats:
                lr = last_run_stats[q]
                row["fetched"] = lr.get("fetched", "")
                row["after include"] = lr.get("after_include", "")
                row["after exclude"] = lr.get("after_exclude", "")
                row["new to pool"] = lr.get("new_to_pool", "")
            rows_query.append(row)

        if rows_query:
            query_df = pd.DataFrame(rows_query).set_index("source id")
            st.dataframe(query_df, use_container_width=True)

    st.divider()

    # reminders
    st.subheader("Reminders")
    col1, col2 = st.columns(2)
    with col1:
        st.metric("Matched jobs awaiting review", unreviewed)
    with col2:
        today = date.today()
        urgent = []
        for r in rows:
            if r.get("review_decision") == "accepted" and r.get("deadline"):
                try:
                    dl = date.fromisoformat(r["deadline"])
                    days_left = (dl - today).days
                    if 0 <= days_left <= 7:
                        urgent.append((r["title"], r["company"], days_left))
                except ValueError:
                    pass
        st.metric("Accepted jobs due within 7 days", len(urgent))
        for title, company, days_left in urgent:
            st.warning(f"{title} @ {company} — {days_left} day(s) left")

    # daily evaluation volume, last 7 days
    if EVAL_LOG.exists():
        st.subheader("Evaluations, last 7 days")
        with EVAL_LOG.open(encoding="utf-8") as f:
            eval_rows = list(csv.DictReader(f))
        daily: dict[str, int] = {}
        for r in eval_rows:
            d = r.get("evaluated_at", "")[:10]
            if d:
                daily[d] = daily.get(d, 0) + 1
        sorted_days = sorted(daily.keys(), reverse=True)[:7]
        if sorted_days:
            df2 = pd.DataFrame({"date": sorted_days,
                                "evaluations": [daily[d] for d in sorted_days]})
            st.bar_chart(df2.set_index("date"))


# ── Tab 2: Review board ───────────────────────────────────
def tab_review():
    st.header("Review board")

    all_rows = load_index()
    matched_rows = [r for r in all_rows if str(r.get("matched", "")).lower() == "true"]

    if not matched_rows:
        st.info("No matched jobs yet")
        return

    # filters
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        directions = ["All"] + sorted(
            {r.get("direction", "") for r in matched_rows} - {""}
        )
        dir_filter = st.selectbox("Type", directions)
    with col2:
        dec_filter = st.selectbox("Review status", ["Undecided", "All", "Accepted", "Ignored"])
    with col3:
        legit_filter = st.selectbox("Legitimacy", ["All", "pass", "review", "reject"])
    with col4:
        score_min = st.slider("Min fit score", 0, 10, 0)

    filtered = matched_rows
    if dir_filter != "All":
        filtered = [r for r in filtered if r.get("direction", "") == dir_filter]
    if dec_filter == "Undecided":
        filtered = [r for r in filtered if not r.get("review_decision")]
    elif dec_filter == "Accepted":
        filtered = [r for r in filtered if r.get("review_decision") == "accepted"]
    elif dec_filter == "Ignored":
        filtered = [r for r in filtered if r.get("review_decision") == "rejected"]
    if legit_filter != "All":
        filtered = [r for r in filtered if r.get("legit_verdict") == legit_filter]
    filtered = [r for r in filtered if float(r.get("score") or 0) >= score_min]
    filtered = sorted(filtered, key=lambda r: float(r.get("score") or 0), reverse=True)

    st.caption(f"Showing {len(filtered)} / {len(matched_rows)} matched jobs")

    for row in filtered:
        job_id          = row["id"]
        title           = row.get("title", "")
        company         = row.get("company", "")
        score           = row.get("score", "?")
        deadline        = row.get("deadline", "—")
        direction       = row.get("direction", "")
        source          = row.get("source", "auto")
        review_decision = row.get("review_decision", "")
        review_reason   = row.get("review_reason", "")
        ws_created      = str(row.get("workspace_created", "")).lower() == "true"
        legit_verdict   = row.get("legit_verdict", "")
        legit_score     = row.get("legit_score", "")

        source_tag = "manual" if source == "manual" else "auto"
        dec_icon = {"accepted": "✅", "rejected": "❌"}.get(review_decision, "📥")
        legit_icon = {"pass": "🟢", "review": "🟡", "reject": "🔴"}.get(legit_verdict, "")

        legit_label = f" | {legit_icon} {legit_verdict} {legit_score}" if legit_verdict else ""

        with st.expander(
            f"{dec_icon} [{direction}] **{title}** @ {company} "
            f"| fit {score} | due {deadline}{legit_label} | {source_tag}",
            expanded=(not review_decision),
        ):
            col_left, col_right = st.columns([2, 1])

            with col_left:
                st.write(f"**Company**: {company}")
                st.write(f"**Fit score**: {score}/10 ({row.get('score_source', 'claude')})")
                if legit_verdict:
                    st.write(f"**Legitimacy**: {legit_icon} {legit_verdict.upper()} "
                             f"{legit_score}/10")
                    if row.get("legit_green_flags"):
                        st.write(f"**Green flags**: {row['legit_green_flags']}")
                    if row.get("legit_red_flags"):
                        st.write(f"**Red flags**: {row['legit_red_flags']}")
                if row.get("deadline"):
                    st.write(f"**Deadline**: {row['deadline']}")
                if row.get("visa_ok"):
                    visa_icon = "✅" if str(row["visa_ok"]).lower() == "true" else "❌"
                    st.write(f"**Eligibility**: {visa_icon} {row.get('visa_note', '')}")
                if row.get("reason"):
                    st.write(f"**Fit reason**: {row['reason']}")
                if row.get("research_fit"):
                    st.write(f"**Research fit**: {row['research_fit']}")
                if row.get("url"):
                    st.link_button("View posting ↗", row["url"])
                if row.get("contact_email"):
                    st.write(f"**Contact**: {row['contact_email']}")
                st.write(f"**Apply via**: {row.get('application_method', '—')}")

            with col_right:
                decision_options = ["— (undecided)", "✅ Accept", "❌ Ignore"]
                current_dec_idx = (
                    1 if review_decision == "accepted" else
                    2 if review_decision == "rejected" else
                    0
                )
                decision = st.radio(
                    "Decision",
                    decision_options,
                    index=current_dec_idx,
                    key=f"dec_{job_id}",
                    label_visibility="collapsed",
                )

                reason = st.text_area(
                    "Reason",
                    value=review_reason,
                    placeholder="Reason for accepting / ignoring",
                    key=f"reason_{job_id}",
                    height=80,
                    label_visibility="collapsed",
                )

                if st.button("💾 Save", key=f"save_{job_id}", type="primary"):
                    new_decision = (
                        "accepted" if "Accept" in decision else
                        "rejected" if "Ignore" in decision else
                        ""
                    )
                    update_row(job_id, {
                        "review_decision": new_decision,
                        "review_reason":   reason,
                    })
                    st.success("Saved")
                    st.rerun()

                # build-workspace button (only for accepted, not-yet-built)
                if review_decision == "accepted" and not ws_created:
                    st.divider()
                    if st.button("📁 Build workspace", key=f"ws_{job_id}"):
                        result = subprocess.run(
                            [sys.executable, str(ROOT / "scripts" / "workspace.py"),
                             "--job-id", job_id],
                            cwd=str(ROOT), capture_output=True, text=True
                        )
                        if result.returncode == 0:
                            update_row(job_id, {
                                "workspace_created": True,
                                "status": "ready",
                            })
                            st.success("Workspace created — open TASK.md in Claude Code to generate CL/CV")
                            st.rerun()
                        else:
                            st.error(f"Failed: {result.stderr}")
                elif ws_created:
                    st.caption("📁 Workspace exists")


# ── Tab 3: Application tracking ───────────────────────────
def tab_tracking():
    col_title, col_btn = st.columns([6, 1])
    col_title.header("Application tracking")
    if col_btn.button("🔄 Refresh", key="tracking_refresh"):
        st.rerun()

    all_rows = load_index()
    accepted = [r for r in all_rows if r.get("review_decision") == "accepted"]

    if not accepted:
        st.info("No accepted jobs yet")
        return

    STATUS_LIST = ["pending", "ignore", "ready", "applying",
                   "applied", "interview", "rejected", "offer"]
    STATUS_EMOJI = {
        "pending":   "📥", "ignore":    "⏭️", "ready":     "✅",
        "applying":  "✏️",  "applied":   "📤", "interview": "🎯",
        "rejected":  "❌", "offer":     "🎉",
    }

    today = date.today()

    for row in sorted(accepted, key=lambda r: r.get("deadline", "9999")):
        job_id   = row["id"]
        title    = row.get("title", "")
        company  = row.get("company", "")
        status   = row.get("status", "pending")
        deadline = row.get("deadline", "")
        applied_date = row.get("applied_date", "")

        # deadline countdown
        days_left_str = ""
        urgent = False
        if deadline:
            try:
                dl = date.fromisoformat(deadline)
                days_left = (dl - today).days
                days_left_str = (f"{days_left} day(s) left" if days_left >= 0
                                 else f"overdue {-days_left} day(s)")
                urgent = 0 <= days_left <= 7
            except ValueError:
                pass

        emoji = STATUS_EMOJI.get(status, "")
        header = f"{emoji} **{title}** @ {company}"
        if days_left_str:
            header += f" | {days_left_str}"

        with st.expander(header, expanded=urgent):
            col1, col2 = st.columns([1, 2])

            with col1:
                st.write(f"**Status**: {status}")
                if applied_date:
                    waiting = row.get("waiting_days", "")
                    st.write(f"**Applied**: {applied_date} (waiting {waiting} day(s))")
                if deadline:
                    st.write(f"**Deadline**: {deadline}")
                if row.get("url"):
                    st.link_button("Posting ↗", row["url"])

            with col2:
                new_status = st.selectbox(
                    "Change status",
                    STATUS_LIST,
                    index=STATUS_LIST.index(status) if status in STATUS_LIST else 0,
                    key=f"track_status_{job_id}",
                )
                new_applied = st.text_input(
                    "Applied date (YYYY-MM-DD)",
                    value=applied_date,
                    key=f"track_applied_{job_id}",
                )
                new_result = st.text_input(
                    "Result",
                    value=row.get("result", ""),
                    key=f"track_result_{job_id}",
                )
                new_notes = st.text_area(
                    "Notes",
                    value=row.get("notes", ""),
                    key=f"track_notes_{job_id}",
                    height=60,
                )

                if st.button("💾 Save", key=f"track_save_{job_id}", type="primary"):
                    waiting_days = ""
                    if new_applied:
                        try:
                            waiting_days = str(
                                (today - date.fromisoformat(new_applied)).days
                            )
                        except ValueError:
                            pass
                    update_row(job_id, {
                        "status":       new_status,
                        "applied_date": new_applied,
                        "waiting_days": waiting_days,
                        "result":       new_result,
                        "notes":        new_notes,
                    })
                    st.success("Saved")
                    st.rerun()


# ── main ──────────────────────────────────────────────────
def main():
    st.title("Job Review")

    tab1, tab2, tab3 = st.tabs(["📊 Overview", "🔍 Review board", "📤 Application tracking"])

    with tab1:
        tab_overview()
    with tab2:
        tab_review()
    with tab3:
        tab_tracking()


if __name__ == "__main__":
    main()
