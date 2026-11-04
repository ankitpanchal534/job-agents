from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import streamlit as st

from jobagent.applicant import Applicant
from jobagent.browser_hands import BrowserHands
from jobagent.config import (
    DEFAULT_PREFERENCES,
    DEFAULT_SETTINGS,
    check_config,
    db_path,
    load_env,
    load_preferences,
    load_settings,
)
from jobagent.fetch import fetch_jd_text
from jobagent.llm import ChatClient
from jobagent.matcher import Matcher, match_job
from jobagent.notify import Notifier, build_channels
from jobagent.profile import DEFAULT_PROFILE_DIR, load_profile, profile_warnings
from jobagent.resolver import Resolver
from jobagent.storage import Answer, Block, Job, JourneyStep, Storage


def get_storage(custom_path: Optional[str] = None) -> Storage:
    path = custom_path or os.environ.get("JOBAGENT_DB") or str(db_path(load_settings()))
    return Storage(path)


def get_metrics(storage: Storage) -> Dict[str, int]:
    all_jobs = storage.list_jobs()
    counts = {
        "total": len(all_jobs),
        "applied": 0,
        "queued": 0,
        "blocked": 0,
        "watch_later": 0,
        "rejected": 0,
        "new": 0,
    }
    for job in all_jobs:
        if job.status in counts:
            counts[job.status] += 1
    return counts


def filter_jobs(
    jobs: List[Job],
    status: Optional[str] = None,
    search: Optional[str] = None,
) -> List[Job]:
    result = jobs
    if status and status.lower() != "all":
        result = [j for j in result if j.status.lower() == status.lower()]
    if search and search.strip():
        term = search.strip().lower()
        result = [
            j for j in result
            if term in (j.title or "").lower()
            or term in (j.company or "").lower()
            or term in (j.url or "").lower()
        ]
    return result


def main() -> None:
    st.set_page_config(
        page_title="JobAgent Control Center",
        page_icon="🤖",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    env = load_env()
    settings = load_settings()
    preferences = load_preferences()
    profile = load_profile()
    storage = get_storage()
    client = ChatClient(settings, env=env)
    resolver = Resolver(profile, preferences, settings, llm=client, extra_bank=storage.approved_answers())
    matcher = Matcher(profile, preferences, settings, client)
    active_channels, _ = build_channels(settings)
    notifier = Notifier(storage=storage, channels=active_channels, cooldown_minutes=settings.notifications.cooldown_minutes)

    # -------------------------------------------------------------
    # SIDEBAR: Resume Management
    # -------------------------------------------------------------
    resumes_dir = Path("profile/resumes")
    resumes_dir.mkdir(parents=True, exist_ok=True)
    existing_resumes = sorted([p for p in resumes_dir.iterdir() if p.suffix.lower() == ".pdf" and p.is_file()])

    with st.sidebar:
        st.header("📄 Resume Management")
        active_resume_path: Optional[Path] = None
        if existing_resumes:
            resume_names = [r.name for r in existing_resumes]
            selected_name = st.selectbox(
                "Active Resume to Attach:",
                resume_names,
                index=0,
                help="This PDF will be automatically uploaded to application forms.",
            )
            active_resume_path = resumes_dir / selected_name
            file_size_kb = active_resume_path.stat().st_size / 1024
            st.caption(f"📁 `{selected_name}` ({file_size_kb:.1f} KB)")
        else:
            st.warning("⚠️ No resume PDF uploaded yet!")

        st.markdown("---")
        st.subheader("Upload Resume")
        uploaded_pdf = st.file_uploader(
            "Upload your resume PDF:",
            type=["pdf"],
            help="Upload your PDF resume to attach to job applications.",
        )
        if uploaded_pdf is not None:
            dest_file = resumes_dir / uploaded_pdf.name
            dest_file.write_bytes(uploaded_pdf.getbuffer())
            st.success(f"Saved: {uploaded_pdf.name}")
            time.sleep(1)
            st.rerun()

    st.title("🤖 JobAgent Control Center")
    st.caption("Privacy-first, autonomous multi-agent job matcher, form resolver, and visible browser applicant.")

    # Top Metric Bar
    metrics = get_metrics(storage)
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total Tracked", metrics["total"])
    col2.metric("✅ Applied", metrics["applied"])
    col3.metric("⏳ In Queue", metrics["queued"])
    col4.metric("⚠️ Blocked / Stuck", metrics["blocked"])
    col5.metric("👀 Watch Later", metrics["watch_later"])

    tab_run, tab_tracker, tab_stuck, tab_system = st.tabs([
        "🚀 Apply & Live Runner",
        "📋 Applications Tracker",
        "⚠️ Incidents & Stuck Queue",
        "⚙️ System & Diagnostics",
    ])

    # -------------------------------------------------------------
    # TAB 1: Apply & Live Runner
    # -------------------------------------------------------------
    with tab_run:
        st.subheader("Apply to Job Postings")
        urls_input = st.text_area(
            "Enter Job URLs (one per line):",
            height=120,
            placeholder="https://jobs.lever.co/example/1234\nhttps://boards.greenhouse.io/example/jobs/5678",
        )

        c_opt1, c_opt2, c_opt3, c_opt4 = st.columns(4)
        dry_run = c_opt1.checkbox("Dry-run (pause before submit)", value=True)
        visible_chrome = c_opt2.checkbox("Visible Chrome & Agent Hands", value=True)
        run_matcher_first = c_opt3.checkbox("Score match first", value=True)
        wait_for_human = c_opt4.checkbox("Keep Chrome open & wait on block", value=True)

        if active_resume_path:
            st.caption(f"📄 **Active Resume**: `{active_resume_path.name}` (manage in sidebar)")
        else:
            st.warning("⚠️ **No resume uploaded!** Upload your PDF resume in the sidebar so the agent can attach it to forms.")

        # Multi-Agent Real-Time Status Cards
        st.markdown("#### 🤖 Multi-Agent Live Status")
        m_c1, m_c2, m_c3, m_c4 = st.columns(4)

        with m_c1:
            st.markdown(
                """<div style="border: 1px solid #3b82f6; border-radius: 8px; padding: 10px; min-height: 140px;">
                <b style="color: #3b82f6;">🔍 Extractor Agent</b><br>
                <small id="extractor-status">Status: <b>Idle</b></small><br>
                <div id="extractor-details" style="font-size: 12px; margin-top: 6px; color: #94a3b8;">
                Awaiting job posting...
                </div>
                </div>""",
                unsafe_allow_html=True,
            )

        with m_c2:
            st.markdown(
                """<div style="border: 1px solid #10b981; border-radius: 8px; padding: 10px; min-height: 140px;">
                <b style="color: #10b981;">⚖️ Matcher Agent</b><br>
                <small id="matcher-status">Status: <b>Idle</b></small><br>
                <div id="matcher-details" style="font-size: 12px; margin-top: 6px; color: #94a3b8;">
                Awaiting extraction...
                </div>
                </div>""",
                unsafe_allow_html=True,
            )

        with m_c3:
            st.markdown(
                """<div style="border: 1px solid #f59e0b; border-radius: 8px; padding: 10px; min-height: 140px;">
                <b style="color: #f59e0b;">💡 Resolver Agent</b><br>
                <small id="resolver-status">Status: <b>Idle</b></small><br>
                <div id="resolver-details" style="font-size: 12px; margin-top: 6px; color: #94a3b8;">
                Awaiting form questions...
                </div>
                </div>""",
                unsafe_allow_html=True,
            )

        with m_c4:
            st.markdown(
                """<div style="border: 1px solid #8b5cf6; border-radius: 8px; padding: 10px; min-height: 140px;">
                <b style="color: #8b5cf6;">🖱️ Browser Applicant</b><br>
                <small id="applicant-status">Status: <b>Idle</b></small><br>
                <div id="applicant-details" style="font-size: 12px; margin-top: 6px; color: #94a3b8;">
                Virtual hands ready.
                </div>
                </div>""",
                unsafe_allow_html=True,
            )

        st.markdown("")

        if st.button("🚀 Start Application Agent", type="primary"):
            lines = [line.strip() for line in urls_input.splitlines() if line.strip() and not line.startswith("#")]
            if not lines:
                st.error("Please provide at least one valid URL.")
            else:
                progress_container = st.container()
                card_updates = st.empty()
                log_box = st.empty()
                logs: List[str] = []

                def log_callback(msg: str):
                    timestamp = time.strftime("%H:%M:%S")
                    logs.append(f"[{timestamp}] {msg}")
                    log_box.code("\n".join(logs[-15:]), language="text")

                with progress_container:
                    st.info(f"Processing {len(lines)} job posting(s)...")

                    for idx, url in enumerate(lines, start=1):
                        log_callback(f"--- Job {idx}/{len(lines)}: {url} ---")
                        job = storage.get_job_by_url(url)
                        if not job:
                            job = storage.add_job(url)

                        # Matcher step
                        if run_matcher_first:
                            log_callback("Fetching JD & evaluating match score against profile...")
                            try:
                                jd_text = fetch_jd_text(url)
                                outcome = match_job(
                                    storage=storage,
                                    matcher=matcher,
                                    url=url,
                                    jd_text=jd_text,
                                    force=False,
                                )
                                if outcome.result:
                                    res = outcome.result
                                    score_val = res.score if res.score is not None else 0
                                    log_callback(f"Match result: {res.decision.upper()} (Score: {score_val}/100) - {res.reason}")
                                    if res.decision == "reject":
                                        rejection_msg = f"Match rejected: {res.reason}. (Uncheck 'Score match first' to apply anyway)"
                                        log_callback(f"⚠️ {rejection_msg}")
                                        st.warning(f"Job #{job.id} skipped: {rejection_msg}")
                                        continue
                            except Exception as match_err:
                                log_callback(f"Match evaluation warning: {match_err}")

                        # Browser applicant step
                        log_callback("Launching Chrome browser with Agent Hands (virtual mouse & keyboard)...")
                        hands = BrowserHands(enabled=visible_chrome)
                        applicant = Applicant(
                            storage=storage,
                            resolver=resolver,
                            notifier=notifier,
                            headless=not visible_chrome,
                            resume_path=active_resume_path,
                            hands=hands,
                            wait_for_human=wait_for_human,
                            on_log=log_callback,
                        )

                        outcome = applicant.apply(job_id=job.id, dry_run=dry_run, wait_for_human=wait_for_human)
                        log_callback(f"Application finished: {outcome.status.upper()} - {outcome.detail}")

                    st.success("✅ Application processing finished! Check the live logs above and the Applications Tracker tab.")

    # -------------------------------------------------------------
    # TAB 2: Applications Tracker & Journey Audit
    # -------------------------------------------------------------
    with tab_tracker:
        st.subheader("Applications Tracker & Complete Journey Audit")

        f_col1, f_col2 = st.columns([1, 2])
        status_filter = f_col1.selectbox("Filter Status:", ["All", "applied", "queued", "blocked", "watch_later", "rejected"])
        search_filter = f_col2.text_input("Search (Company, Title, or URL):", "")

        all_jobs = storage.list_jobs()
        filtered = filter_jobs(all_jobs, status=status_filter, search=search_filter)

        st.caption(f"Showing {len(filtered)} of {len(all_jobs)} tracked postings.")

        if not filtered:
            st.info("No applications match the selected filter.")
        else:
            for j in reversed(filtered):
                score_str = f"Score: {int(j.score)}%" if j.score is not None else "No score"
                with st.expander(f"#{j.id} | {j.company or 'Unknown Company'} — {j.title or 'Job Posting'} [{j.status.upper()}] ({score_str})"):
                    c_left, c_right = st.columns([3, 1])
                    with c_left:
                        st.markdown(f"**URL**: [{j.url}]({j.url})")
                        st.markdown(f"**Summary**: {j.jd_summary or 'No summary recorded.'}")
                        st.caption(f"Created: {j.created_at} | Last Updated: {j.updated_at}")
                    with c_right:
                        st.markdown(f"**Status**: `{j.status}`")
                        if st.button("Apply Now (Agent Hands)", key=f"apply_{j.id}"):
                            with st.spinner(f"Launching Chrome and applying to Job #{j.id}..."):
                                app = Applicant(
                                    storage=storage,
                                    resolver=resolver,
                                    notifier=notifier,
                                    headless=False,
                                    resume_path=active_resume_path,
                                    wait_for_human=True,
                                    on_log=st.toast,
                                )
                                app_res = app.apply(job_id=j.id, dry_run=False, wait_for_human=True)
                                st.info(f"Result: {app_res.status.upper()} - {app_res.detail}")
                                time.sleep(1)
                                st.rerun()

                    # -------------------------------------------------------------
                    # Full Application Journey Timeline
                    # -------------------------------------------------------------
                    st.markdown("---")
                    st.markdown("#### 📜 Complete Application Journey")
                    journey_steps = storage.list_journey_steps(j.id)
                    if not journey_steps:
                        st.caption("No journey audit steps recorded yet for this application.")
                    else:
                        for step_idx, step in enumerate(journey_steps, start=1):
                            # Icon & badge based on status
                            if step.status == "success":
                                status_badge = "🟢 Success"
                            elif step.status == "blocked":
                                status_badge = "🔴 Blocked"
                            elif step.status == "warning":
                                status_badge = "🟠 Warning"
                            elif step.status == "failed":
                                status_badge = "❌ Failed"
                            else:
                                status_badge = "🔵 Info"

                            # Agent icon
                            agent_icon = "🤖"
                            if "Extractor" in step.agent:
                                agent_icon = "🔍"
                            elif "Matcher" in step.agent:
                                agent_icon = "⚖️"
                            elif "Resolver" in step.agent:
                                agent_icon = "💡"
                            elif "Applicant" in step.agent:
                                agent_icon = "🖱️"

                            col_icon, col_content = st.columns([1, 11])
                            with col_icon:
                                st.markdown(f"<h3 style='margin:0;'>{agent_icon}</h3>", unsafe_allow_html=True)
                            with col_content:
                                st.markdown(f"**Step {step_idx}: {step.agent}** &nbsp; `{status_badge}` &nbsp; <small style='color:gray;'>{step.created_at}</small>", unsafe_allow_html=True)
                                st.markdown(f"{step.summary}")
                                if step.details:
                                    with st.expander("View Details & Payloads", expanded=False):
                                        st.json(step.details)

    # -------------------------------------------------------------
    # TAB 3: Incident & Stuck Queue
    # -------------------------------------------------------------
    with tab_stuck:
        st.subheader("Human-in-the-Loop Incidents & Stuck Jobs")

        open_blocks = storage.list_blocks()
        if not open_blocks:
            st.success("🎉 No jobs are currently stuck or blocked!")
        else:
            st.warning(f"⚠️ {len(open_blocks)} job(s) require your attention:")
            for b in open_blocks:
                job = storage.get_job(b.job_id)
                with st.container():
                    st.markdown(f"### Block on Job #{b.job_id} ({b.reason.upper()})")
                    st.markdown(f"**Detail**: {b.detail}")
                    if job:
                        st.markdown(f"**Job URL**: [{job.url}]({job.url})")
                    st.caption(f"Host: {b.host} | Time: {b.created_at}")

                    if b.screenshot_path and os.path.exists(b.screenshot_path):
                        st.image(b.screenshot_path, caption="Incident Screenshot", use_container_width=True)

                    btn_c1, btn_c2 = st.columns(2)
                    if btn_c1.button(f"Resume Job #{b.job_id} (Unblock & Continue)", key=f"resume_{b.id}", type="primary"):
                        storage.resume_job(b.job_id)
                        st.success(f"Job #{b.job_id} unblocked! If a browser session is waiting, it will resume immediately.")
                        time.sleep(1)
                        st.rerun()

                    # Show journey up to block
                    with st.expander("View Journey for this blocked job"):
                        b_steps = storage.list_journey_steps(b.job_id)
                        for s in b_steps:
                            st.markdown(f"- **{s.agent}** ({s.status}): {s.summary}")

        st.divider()
        st.subheader("Review Question Bank & Pending Drafts")
        needs_input = storage.list_answers(status="needs_input")
        drafts = storage.list_answers(status="pending_review")

        if not needs_input and not drafts:
            st.info("No questions currently waiting for review.")

        if needs_input:
            st.markdown(f"#### Questions Needing Answers ({len(needs_input)})")
            for item in needs_input:
                st.markdown(f"**{item.question}**")
                ans_text = st.text_area("Your Answer:", key=f"input_{item.id}")
                if st.button("Save & Approve", key=f"save_{item.id}", type="primary"):
                    if ans_text.strip():
                        storage.approve_answer(item.id, final_answer=ans_text.strip())
                        st.success("Answer saved to curated bank!")
                        st.rerun()
                    else:
                        st.error("Please enter an answer before saving.")

        if drafts:
            st.markdown(f"#### Model Drafts to Review ({len(drafts)})")
            for item in drafts:
                st.markdown(f"**{item.question}**")
                edit_text = st.text_area("Edit Draft:", value=item.answer or "", key=f"draft_{item.id}")
                c_app, c_rej = st.columns(2)
                if c_app.button("Approve Draft", key=f"app_{item.id}", type="primary"):
                    storage.approve_answer(item.id, final_answer=edit_text.strip())
                    st.rerun()
                if c_rej.button("Reject Draft", key=f"rej_{item.id}"):
                    storage.reject_answer(item.id)
                    st.rerun()

    # -------------------------------------------------------------
    # TAB 4: System & Diagnostics
    # -------------------------------------------------------------
    with tab_system:
        st.subheader("System Health & Diagnostic Tools")

        c1, c2, c3 = st.columns(3)

        if c1.button("🔍 Check Configuration"):
            warns = profile_warnings(profile, preferences)
            if warns:
                st.warning(f"{len(warns)} Profile Warnings:")
                for w in warns:
                    st.write(f"- {w}")
            else:
                st.success("✅ Configuration & Profile complete! Zero warnings.")

        if c2.button("🧠 Test LLM Connection"):
            try:
                task = settings.llm.tasks.get("jd_summary")
                provider = task.provider or settings.llm.provider
                start_t = time.perf_counter()
                reply = client.complete("jd_summary", "respond with 'OK'", "test")
                duration = time.perf_counter() - start_t
                st.success(f"✅ LLM Connected ({provider}): reply='{reply.strip()}' in {duration:.2f}s")
            except Exception as e:
                st.error(f"❌ LLM Test Failed: {e}")

        if c3.button("🔔 Send Test Notification"):
            try:
                notifier.notify_test()
                st.success("✅ Test push notification sent to ntfy!")
            except Exception as e:
                st.error(f"❌ Notification Failed: {e}")

        st.divider()
        st.markdown("### Active Settings Overview")
        st.json({
            "llm_provider": settings.llm.provider,
            "base_url": settings.llm.providers[settings.llm.provider].base_url,
            "apply_threshold": settings.matcher.apply_threshold,
            "watch_floor": settings.matcher.watch_floor,
            "database_path": str(storage._path),
        })


if __name__ == "__main__":
    main()
