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
from jobagent.profile import DEFAULT_PROFILE_DIR, load_profile, profile_warnings, save_learned_answer
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

        with st.expander("🌐 Chrome Profile & Session Settings", expanded=False):
            p_c1, p_c2 = st.columns([1, 1])
            with p_c1:
                profile_name_input = st.text_input("Chrome Profile Name:", value="jobagent", help="Dedicated Chrome profile name (defaults to 'jobagent')")
                profile_mode = st.selectbox(
                    "Chrome Session Mode:",
                    [
                        f"Persistent '{profile_name_input}' Profile (Saves all logins & cookies)",
                        "Connect to Running Chrome (CDP Port 9222)",
                        "Temporary Guest / Incognito Profile",
                    ],
                    index=0,
                    help=(
                        "Persistent Profile (Recommended): Runs Chrome with a dedicated user data profile (data/chrome_profile). "
                        "All logins (Google, LinkedIn, Lever, Greenhouse) are permanently preserved across runs so you never have to re-login.\n\n"
                        "Connect to Running Chrome: Connects directly to your already open Chrome browser window and tabs (requires Chrome launched with --remote-debugging-port=9222).\n\n"
                        "Temporary Guest Profile: Opens an empty throwaway browser session that is deleted when closed."
                    ),
                )
            with p_c2:
                if "Connect to Running" in profile_mode:
                    cdp_input = st.text_input("Remote Debugging URL:", value="http://localhost:9222")
                else:
                    cdp_input = ""
                    if "Persistent" in profile_mode:
                        st.caption(f"📁 Profile: `{profile_name_input}` stored in `data/chrome_profile`")

            if "Connect to Running" in profile_mode:
                st.info(
                    "💡 **How to connect JobAgent directly to your open Chrome browser:**\n\n"
                    "1. Completely quit Chrome (`Cmd+Q` on Mac)\n"
                    "2. Start Chrome from Terminal with remote debugging:\n"
                    f"   ```bash\n"
                    f"   python -m jobagent chrome --profile-directory=\"{profile_name_input}\"\n"
                    "   ```\n"
                    f"3. JobAgent will now operate directly inside your active Chrome window under the **'{profile_name_input}'** profile with all your open tabs, history, and logins!"
                )

        if active_resume_path:
            st.caption(f"📄 **Active Resume**: `{active_resume_path.name}` (manage in sidebar)")
        else:
            st.warning("⚠️ **No resume uploaded!** Upload your PDF resume in the sidebar so the agent can attach it to forms.")

        # Multi-Agent Real-Time Status Cards
        st.markdown("#### 🤖 Multi-Agent Live Status")
        m_c1, m_c2, m_c3, m_c4 = st.columns(4)
        card_ext = m_c1.empty()
        card_mat = m_c2.empty()
        card_res = m_c3.empty()
        card_app = m_c4.empty()

        def render_card_extractor(
            status: str,
            title: str = "",
            company: str = "",
            details: str = "",
            **kwargs,
        ):
            status_colors = {
                "idle": "#94a3b8",
                "extracting...": "#f59e0b",
                "completed": "#10b981",
                "failed": "#ef4444",
            }
            color = status_colors.get(status.lower(), "#38bdf8")
            badge_bg = f"{color}22"
            card_ext.markdown(
                f"""<div style="border: 1px solid {color}; border-radius: 8px; padding: 12px; min-height: 160px; background-color: rgba(255,255,255,0.02);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <b style="color: #38bdf8; font-size: 14px;">🔍 Extractor Agent</b>
                        <span style="background: {badge_bg}; color: {color}; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; border: 1px solid {color}44;">{status}</span>
                    </div>
                    <div style="font-size: 13px; font-weight: 600; color: #f1f5f9; margin-bottom: 4px;">{company or '—'}</div>
                    <div style="font-size: 12px; color: #cbd5e1; margin-bottom: 6px;">{title or 'No active job'}</div>
                    <div style="font-size: 11px; color: #94a3b8; line-height: 1.4;">{details or 'Awaiting job posting...'}</div>
                </div>""",
                unsafe_allow_html=True,
            )

        def render_card_matcher(
            status: str,
            score: Optional[float] = None,
            decision: str = "",
            reason: str = "",
            details: str = "",
            matched: list[str] = [],
            missing: list[str] = [],
            **kwargs,
        ):
            status_colors = {
                "idle": "#94a3b8",
                "scoring...": "#f59e0b",
                "apply": "#10b981",
                "watch_later": "#f59e0b",
                "reject": "#ef4444",
                "rejected": "#ef4444",
                "completed": "#10b981",
                "failed": "#ef4444",
            }
            color = status_colors.get(decision.lower(), status_colors.get(status.lower(), "#10b981"))
            badge_bg = f"{color}22"
            score_disp = f"<b style='font-size: 16px; color: {color};'>{int(score)}%</b>" if score is not None else "<span style='color: #94a3b8;'>—</span>"
            decision_label = decision.upper() if decision else status
            effective_reason = reason or details

            matched_pills = "".join(f"<span style='background: #10b98122; color: #10b981; border: 1px solid #10b98144; padding: 1px 5px; border-radius: 4px; font-size: 10px; margin-right: 3px;'>✓ {s}</span>" for s in (matched or [])[:3])
            missing_pills = "".join(f"<span style='background: #ef444422; color: #ef4444; border: 1px solid #ef444444; padding: 1px 5px; border-radius: 4px; font-size: 10px; margin-right: 3px;'>✗ {s}</span>" for s in (missing or [])[:2])

            card_mat.markdown(
                f"""<div style="border: 1px solid {color}; border-radius: 8px; padding: 12px; min-height: 160px; background-color: rgba(255,255,255,0.02);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <b style="color: #10b981; font-size: 14px;">⚖️ Matcher Agent</b>
                        <span style="background: {badge_bg}; color: {color}; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; border: 1px solid {color}44;">{decision_label}</span>
                    </div>
                    <div style="display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; font-size: 12px; color: #cbd5e1;">
                        <span>Match Score:</span> {score_disp}
                    </div>
                    <div style="margin-bottom: 6px; line-height: 1.6;">{matched_pills} {missing_pills}</div>
                    <div style="font-size: 11px; color: #94a3b8; line-height: 1.3;">{effective_reason[:80] + ('...' if len(effective_reason) > 80 else '') if effective_reason else 'Awaiting extraction...'}</div>
                </div>""",
                unsafe_allow_html=True,
            )

        def render_card_resolver(
            status: str,
            question: str = "",
            answer: str = "",
            source: str = "",
            details: str = "",
            **kwargs,
        ):
            status_colors = {
                "idle": "#94a3b8",
                "resolving...": "#f59e0b",
                "resolved": "#10b981",
                "curated": "#10b981",
                "generated": "#3b82f6",
                "needs_input": "#ef4444",
                "needs input": "#ef4444",
                "answered": "#10b981",
                "waiting on user": "#ef4444",
                "waiting for human": "#ef4444",
                "human in chrome": "#10b981",
            }
            color = status_colors.get(source.lower(), status_colors.get(status.lower(), "#f59e0b"))
            badge_bg = f"{color}22"
            effective_q = question or details
            q_disp = effective_q[:45] + ("..." if len(effective_q) > 45 else "") if effective_q else "Awaiting form questions..."
            a_disp = answer[:35] + ("..." if len(answer) > 35 else "") if answer else "—"

            card_res.markdown(
                f"""<div style="border: 1px solid {color}; border-radius: 8px; padding: 12px; min-height: 160px; background-color: rgba(255,255,255,0.02);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <b style="color: #f59e0b; font-size: 14px;">💡 Resolver Agent</b>
                        <span style="background: {badge_bg}; color: {color}; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; border: 1px solid {color}44;">{status}</span>
                    </div>
                    <div style="font-size: 11px; color: #94a3b8; margin-bottom: 3px;">Active Question:</div>
                    <div style="font-size: 12px; font-weight: 600; color: #f1f5f9; margin-bottom: 6px; line-height: 1.3;">{q_disp}</div>
                    <div style="font-size: 11px; color: #94a3b8;">Answer: <span style="color: #cbd5e1;">{a_disp}</span></div>
                    <div style="font-size: 10px; color: #64748b; margin-top: 4px;">Source: {source or 'profile/answers.yaml'}</div>
                </div>""",
                unsafe_allow_html=True,
            )

        def render_card_applicant(
            status: str,
            action: str = "",
            target: str = "",
            hands_state: str = "Virtual Hands Ready",
            details: str = "",
            **kwargs,
        ):
            status_colors = {
                "idle": "#94a3b8",
                "navigating": "#3b82f6",
                "typing": "#a855f7",
                "clicking": "#8b5cf6",
                "attaching": "#3b82f6",
                "selecting": "#8b5cf6",
                "checking": "#8b5cf6",
                "submitting": "#3b82f6",
                "verified": "#10b981",
                "waiting for human": "#ef4444",
                "answered": "#10b981",
                "completed": "#10b981",
                "applied": "#10b981",
                "blocked": "#ef4444",
                "failed": "#ef4444",
                "running": "#8b5cf6",
            }
            color = status_colors.get(status.lower(), "#8b5cf6")
            badge_bg = f"{color}22"
            effective_action = action or details
            act_disp = effective_action[:45] + ("..." if len(effective_action) > 45 else "") if effective_action else "Virtual hands standby."
            tgt_disp = target[:35] + ("..." if len(target) > 35 else "") if target else "—"

            card_app.markdown(
                f"""<div style="border: 1px solid {color}; border-radius: 8px; padding: 12px; min-height: 160px; background-color: rgba(255,255,255,0.02);">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <b style="color: #8b5cf6; font-size: 14px;">🖱️ Browser Applicant</b>
                        <span style="background: {badge_bg}; color: {color}; padding: 2px 8px; border-radius: 9999px; font-size: 11px; font-weight: 600; border: 1px solid {color}44;">{status}</span>
                    </div>
                    <div style="font-size: 11px; color: #94a3b8; margin-bottom: 3px;">Current Action:</div>
                    <div style="font-size: 12px; font-weight: 600; color: #f1f5f9; margin-bottom: 6px; line-height: 1.3;">{act_disp}</div>
                    <div style="font-size: 11px; color: #94a3b8;">Target: <span style="color: #cbd5e1;">{tgt_disp}</span></div>
                    <div style="font-size: 10px; color: #a855f7; margin-top: 4px;">Hands: {hands_state}</div>
                </div>""",
                unsafe_allow_html=True,
            )

        # Initial Card State
        render_card_extractor("Idle")
        render_card_matcher("Idle")
        render_card_resolver("Idle")
        render_card_applicant("Idle", hands_state="Virtual Hands Ready" if visible_chrome else "Headless Mode")

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

                def resolve_callback(q: str, a: str, src: str):
                    render_card_resolver("Resolved", question=q, answer=a, source=src)

                def action_callback(action: str, target: str, extra: str):
                    render_card_applicant(action, action=f"{action}: {target}", target=target, hands_state=extra)
                    if action == "Waiting for Human":
                        render_card_resolver("Needs Input", question=target, answer="Waiting in Chrome...", source="needs_input")
                    elif action == "Answered":
                        render_card_resolver("Answered", question=target, answer=extra, source="Human in Chrome")

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
                            render_card_extractor("Extracting...", details=f"Reading {url[:35]}...")
                            render_card_matcher("Scoring...", details="Evaluating skills against profile...")
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
                                    render_card_extractor(
                                        "Completed",
                                        title=res.posting.title,
                                        company=res.posting.company,
                                        details=f"Skills: {len(res.posting.must_have_skills)} must-have, {len(res.posting.nice_to_have_skills)} nice-to-have",
                                    )
                                    render_card_matcher(
                                        res.decision.upper(),
                                        score=res.score,
                                        decision=res.decision,
                                        reason=res.reason,
                                        matched=[s for s in res.posting.must_have_skills if s not in res.missing_must_haves],
                                        missing=res.missing_must_haves,
                                    )
                                    log_callback(f"Match result: {res.decision.upper()} (Score: {score_val}/100) - {res.reason}")
                                    if res.decision == "reject":
                                        rejection_msg = f"Match rejected: {res.reason}. (Uncheck 'Score match first' to apply anyway)"
                                        log_callback(f"⚠️ {rejection_msg}")
                                        st.warning(f"Job #{job.id} skipped: {rejection_msg}")
                                        continue
                            except Exception as match_err:
                                log_callback(f"Match evaluation warning: {match_err}")
                                render_card_extractor("Failed", details=str(match_err)[:60])
                                render_card_matcher("Failed", reason=str(match_err)[:60])

                        # Browser applicant step
                        log_callback("Launching Chrome browser with Agent Hands (virtual mouse & keyboard)...")
                        render_card_applicant("Navigating", action="Launching Chrome", target=url, hands_state="Hands Active")
                        hands = BrowserHands(enabled=visible_chrome)
                        chosen_user_data_dir = "data/chrome_profile" if "Persistent" in profile_mode else None
                        chosen_cdp_url = cdp_input.strip() if ("Connect to Running" in profile_mode and cdp_input.strip()) else None

                        applicant = Applicant(
                            storage=storage,
                            resolver=resolver,
                            notifier=notifier,
                            headless=not visible_chrome,
                            resume_path=active_resume_path,
                            hands=hands,
                            wait_for_human=wait_for_human,
                            on_log=log_callback,
                            on_resolve=resolve_callback,
                            on_action=action_callback,
                            user_data_dir=chosen_user_data_dir,
                            cdp_url=chosen_cdp_url,
                            profile_directory=profile_name_input.strip() or "jobagent",
                            llm=client,
                        )

                        outcome = applicant.apply(job_id=job.id, dry_run=dry_run, wait_for_human=wait_for_human)
                        log_callback(f"Application finished: {outcome.status.upper()} - {outcome.detail}")
                        render_card_applicant(outcome.status.upper(), action=outcome.detail, target=url, hands_state="Finished")

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
                                    user_data_dir="data/chrome_profile",
                                    profile_directory="jobagent",
                                    llm=client,
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
                        save_learned_answer(Path("profile/answers.yaml"), item.question, ans_text.strip())
                        st.success("Answer saved to curated bank and profile/answers.yaml!")
                        time.sleep(0.5)
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
                    save_learned_answer(Path("profile/answers.yaml"), item.question, edit_text.strip())
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
