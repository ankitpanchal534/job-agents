from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from jobagent.browser_hands import BrowserHands
from jobagent.form_inspector import (
    FormField,
    extract_form_fields,
    find_best_select_option,
    normalize_field_prompt,
)
from jobagent.notify import Notification, Notifier
from jobagent.profile import DEFAULT_PROFILE_DIR, save_learned_answer
from jobagent.resolver import Resolver
from jobagent.storage import Block, Storage
from jobagent.text import normalize_question


@dataclass(frozen=True)
class ApplicantOutcome:
    status: str  # 'applied', 'ready_to_submit', 'blocked', 'failed'
    detail: str
    job_id: int


class Applicant:
    def __init__(
        self,
        storage: Storage,
        resolver: Resolver,
        notifier: Optional[Notifier] = None,
        headless: bool = False,
        profile_dir: Path = DEFAULT_PROFILE_DIR,
        resume_path: Optional[Path] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str, str], None]] = None,
        hands: Optional[BrowserHands] = None,
        wait_for_human: bool = True,
    ):
        self._storage = storage
        self._resolver = resolver
        self._notifier = notifier
        self._headless = headless
        self._profile_dir = Path(profile_dir)
        self._resume_path = Path(resume_path) if resume_path else None
        self._on_log = on_log or (lambda msg: None)
        self._on_status = on_status or (lambda status, detail: None)
        self._hands = hands if hands is not None else BrowserHands(enabled=not headless)
        self._wait_for_human = wait_for_human

    def _log(self, message: str) -> None:
        self._on_log(message)

    def _status(self, status: str, detail: str) -> None:
        self._on_status(status, detail)

    def _find_resume_pdf(self) -> Optional[Path]:
        if self._resume_path and self._resume_path.is_file():
            return self._resume_path
        resumes_dir = self._profile_dir / "resumes"
        if not resumes_dir.is_dir():
            return None
        for p in sorted(resumes_dir.glob("*.pdf")):
            if p.suffix.lower() == ".pdf" and p.is_file():
                return p
        return None

    def _capture_screenshot(self, page, job_id: int, label: str) -> str:
        screenshots_dir = Path("data/screenshots")
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        ts = int(time.time())
        screenshot_path = screenshots_dir / f"{label}_{job_id}_{ts}.png"
        try:
            page.screenshot(path=str(screenshot_path), full_page=True)
            return str(screenshot_path)
        except Exception:
            return ""

    def _read_element_value(self, page, field: FormField) -> Optional[str]:
        """Reads current value entered into an element on the active page."""
        try:
            if field.input_type == "radio":
                checked = page.locator(f"input[type='radio'][name='{field.name}']:checked").first
                if checked.count() > 0:
                    val = checked.get_attribute("value") or ""
                    if not val or val.lower() in ("on", "true"):
                        lbl_el = checked.locator("xpath=ancestor::label[1]")
                        val = (lbl_el.inner_text() if lbl_el.count() > 0 else "").strip()
                    return val.strip() if val.strip() else None
                return None

            if field.tag == "select":
                sel_loc = page.locator(field.selector).first
                if sel_loc.count() > 0:
                    val = sel_loc.evaluate("""el => {
                        const opt = el.options[el.selectedIndex];
                        return opt ? (opt.text || opt.value) : '';
                    }""")
                    if val and not val.strip().lower().startswith("select") and not val.strip().lower().startswith("choose"):
                        return val.strip()
                return None

            if field.input_type in ("text", "number", "email", "tel") or field.tag == "textarea":
                loc = page.locator(field.selector).first
                if loc.count() > 0:
                    val = loc.input_value()
                    return val.strip() if val and val.strip() else None
                return None
        except Exception:
            return None
        return None

    def _save_learned(self, prompt: str, user_val: str, job_id: Optional[int] = None) -> None:
        """Saves a learned human answer into answers.yaml, SQLite answers bank, and in-memory resolver."""
        if not user_val or user_val == "resumed":
            return
        answers_file = self._profile_dir / "answers.yaml"
        saved = save_learned_answer(answers_file, prompt, user_val)
        if saved:
            self._log(f"✓ Saved '{prompt}': '{user_val}' to profile/answers.yaml for future use.")

        self._storage.add_approved_answer(
            question=prompt,
            answer=user_val,
            job_id=job_id,
        )

        if hasattr(self._resolver, "_extra_bank") and isinstance(self._resolver._extra_bank, dict):
            self._resolver._extra_bank[normalize_question(prompt)] = user_val

    def _fill_field_with_value(self, page, field: FormField, val: str, prompt: str) -> None:
        """Fills an element with a specified value using Agent Hands."""
        if field.input_type == "radio":
            opt = find_best_select_option(field.options, val) or val
            r_loc = page.locator(f"input[type='radio'][name='{field.name}'][value='{opt}']").first
            target_loc = r_loc if r_loc.count() > 0 else page.locator(f"label:has-text('{opt}')").first
            self._hands.click_element(page, target_loc, label=f"Select: {opt}")
        elif field.tag == "select":
            opt = find_best_select_option(field.options, val) or val
            page.locator(field.selector).first.select_option(label=opt)
        else:
            loc = page.locator(field.selector).first
            self._hands.type_into_element(page, loc, val, label=f"Typing {prompt[:12]}...")

    def _wait_for_question_resolution(
        self,
        page,
        job_id: int,
        field: FormField,
        prompt: str,
        timeout_seconds: int = 600,
    ) -> Optional[str]:
        """Keeps Chrome open and active, highlighting the field and waiting for human input."""
        screenshot = self._capture_screenshot(page, job_id, "question")
        detail = f"Blocked on required question needing input: {prompt}"
        self._log(f"BLOCKED: {detail}")

        page_url = page.url if hasattr(page, "url") else None
        block = self._storage.block_job(
            job_id=job_id,
            reason="unknown_question",
            detail=detail,
            page_url=page_url,
            screenshot_path=screenshot,
        )
        self._storage.add_journey_step(
            job_id=job_id,
            step="block_detected",
            agent="BrowserApplicant",
            status="blocked",
            summary=f"Waiting for human input in Chrome for '{prompt}'",
            details={"question": prompt, "screenshot": screenshot, "reason": "unknown_question"},
        )
        if self._notifier:
            self._notifier.notify_block(block)
        self._status("waiting_for_user", f"Waiting for human to answer '{prompt}' in Chrome...")

        # Visually highlight the field in Chrome with a pulsating blue outline and focus it
        try:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) {
                        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                        el.style.outline = '3px solid #2563eb';
                        el.style.boxShadow = '0 0 12px rgba(37, 99, 235, 0.6)';
                        el.focus();
                    }
                }""",
                field.selector,
            )
        except Exception:
            pass

        self._hands.set_label(page, f"Please fill: {prompt[:20]}...")
        self._log(
            f"⏳ AGENT WAITING: Please enter '{prompt}' directly in the open Chrome window.\n"
            f"The agent is actively watching and will automatically resume once answered."
        )

        start_time = time.time()
        last_val = ""
        last_change_time = time.time()

        while time.time() - start_time < timeout_seconds:
            if page.is_closed():
                self._log("Browser was closed by user.")
                return None

            # 1. Check if human answered directly in Chrome
            current_val = self._read_element_value(page, field)
            if current_val:
                if current_val != last_val:
                    last_val = current_val
                    last_change_time = time.time()
                else:
                    # Check if value is stable (user finished typing or blurred field)
                    is_focused = False
                    try:
                        is_focused = page.evaluate("(sel) => document.activeElement === document.querySelector(sel)", field.selector)
                    except Exception:
                        pass

                    time_stable = time.time() - last_change_time
                    if (not is_focused and time_stable >= 0.8) or time_stable >= 1.8:
                        self._on_question_resolved(page, job_id, block, field, prompt, current_val)
                        return current_val

            # 2. Check if user answered via Dashboard / CLI
            if self._storage.is_block_resolved(block.id):
                approved = dict(self._storage.approved_answers())
                val = approved.get(prompt)
                if not val:
                    for q, a in approved.items():
                        if normalize_question(q) == normalize_question(prompt):
                            val = a
                            break
                if val:
                    self._fill_field_with_value(page, field, val, prompt)
                    self._on_question_resolved(page, job_id, block, field, prompt, val)
                    return val
                else:
                    final_val = self._read_element_value(page, field) or "resumed"
                    self._on_question_resolved(page, job_id, block, field, prompt, final_val)
                    return final_val

            page.wait_for_timeout(500)

        self._log(f"Timed out after {timeout_seconds}s waiting for '{prompt}'.")
        return None

    def _on_question_resolved(
        self,
        page,
        job_id: int,
        block: Block,
        field: FormField,
        prompt: str,
        user_val: str,
    ) -> None:
        """Handles post-resolution cleanup, profile saving, and status resumption."""
        try:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (el) {
                        el.style.outline = '';
                        el.style.boxShadow = '';
                    }
                }""",
                field.selector,
            )
        except Exception:
            pass
        self._hands.set_label(page, "")

        self._save_learned(prompt, user_val, job_id=job_id)

        if not self._storage.is_block_resolved(block.id):
            self._storage.resume_job(job_id)

        self._storage.add_journey_step(
            job_id=job_id,
            step="field_input",
            agent="BrowserApplicant",
            status="success",
            summary=f"Human resolved '{prompt}': '{user_val}'. Saved to FAQ document.",
            details={"field": prompt, "value": user_val, "saved_to_profile": True},
        )
        self._log(f"✓ Resuming application flow...")

    def _wait_for_block_resolution(self, page, job_id: int, block: Block, timeout_seconds: int = 600) -> bool:
        """Waits in-place with the browser open for the user to resolve a generic block (e.g. CAPTCHA, unsupported portal)."""
        self._log(
            f"⏳ AGENT BLOCKED: {block.detail}\n"
            f"Keeping Chrome open. Please resolve the issue in browser or click 'Resume' in Dashboard..."
        )
        self._status("waiting_for_user", f"Blocked: {block.detail}. Waiting for human...")
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            if page.is_closed():
                self._log("Browser was closed by user.")
                return False

            # 1. Check if block resolved via dashboard/CLI
            if self._storage.is_block_resolved(block.id):
                self._log("✓ Block resolved via Dashboard/CLI! Resuming in open browser...")
                self._storage.add_journey_step(
                    job_id=job_id,
                    step="block_resolved",
                    agent="BrowserApplicant",
                    status="success",
                    summary="Block marked resolved via dashboard. Resuming in open browser.",
                    details={"block_id": block.id, "reason": block.reason},
                )
                return True

            # 2. Check if CAPTCHA solved directly in browser
            if block.reason == "captcha" and not self._detect_captcha(page):
                self._log("✓ CAPTCHA cleared in browser! Resuming application...")
                self._storage.resume_job(job_id)
                self._storage.add_journey_step(
                    job_id=job_id,
                    step="block_resolved",
                    agent="BrowserApplicant",
                    status="success",
                    summary="CAPTCHA solved in browser. Resuming application.",
                    details={"block_id": block.id},
                )
                return True

            page.wait_for_timeout(1000)

        self._log(f"Timed out after {timeout_seconds}s waiting for human resolution.")
        return False

    def apply(self, job_id: int, dry_run: bool = True, wait_for_human: Optional[bool] = None) -> ApplicantOutcome:
        job = self._storage.get_job(job_id)
        if not job:
            return ApplicantOutcome(status="failed", detail=f"Job #{job_id} not found", job_id=job_id)

        should_wait = self._wait_for_human if wait_for_human is None else wait_for_human
        url = job.url
        self._log(f"Starting application process for Job #{job_id}: {url}")
        self._status("running", f"Opening browser for Job #{job_id}...")

        # Backfill extractor and matcher journey steps if evaluations exist
        existing_steps = self._storage.list_journey_steps(job_id)
        existing_step_names = {s.step for s in existing_steps}
        evals = self._storage.list_evaluations(job_id)
        if evals and "extractor" not in existing_step_names:
            last_eval = evals[-1]
            self._storage.add_journey_step(
                job_id=job_id,
                step="extractor",
                agent="ExtractorAgent",
                status="success",
                summary=f"Extracted role '{job.title or 'N/A'}' at '{job.company or 'N/A'}'",
                details=last_eval.extracted,
            )
        if evals and "matcher" not in existing_step_names:
            last_eval = evals[-1]
            self._storage.add_journey_step(
                job_id=job_id,
                step="matcher",
                agent="MatcherAgent",
                status="success" if last_eval.decision == "apply" else ("warning" if last_eval.decision == "watch_later" else "failed"),
                summary=f"Decision: {last_eval.decision.upper()} (Score: {last_eval.score if last_eval.score is not None else 'N/A'}%) - {last_eval.reason}",
                details={
                    "decision": last_eval.decision,
                    "score": last_eval.score,
                    "missing_must_haves": last_eval.missing_must_haves,
                    "reason": last_eval.reason,
                },
            )

        with sync_playwright() as p:
            launch_kwargs = {"channel": "chrome", "headless": self._headless}
            if not self._headless:
                launch_kwargs["args"] = ["--start-maximized"]

            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(viewport={"width": 1280, "height": 800} if self._headless else None)
            page = context.new_page()

            try:
                self._log(f"Navigating to {url}...")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                self._hands.inject_cursor(page)
                self._storage.add_journey_step(
                    job_id=job_id,
                    step="navigation",
                    agent="BrowserApplicant",
                    status="info",
                    summary=f"Navigated to job URL: {url}",
                    details={"url": url},
                )

                # Check for CAPTCHA / bot challenge
                if self._detect_captcha(page):
                    screenshot = self._capture_screenshot(page, job_id, "captcha")
                    detail = "CAPTCHA or bot verification detected on page."
                    self._log(f"BLOCKED: {detail}")
                    block = self._storage.block_job(
                        job_id=job_id,
                        reason="captcha",
                        detail=detail,
                        page_url=page.url,
                        screenshot_path=screenshot,
                    )
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="block_detected",
                        agent="BrowserApplicant",
                        status="blocked",
                        summary=detail,
                        details={"reason": "captcha", "screenshot": screenshot},
                    )
                    if self._notifier:
                        self._notifier.notify_block(block)
                    self._status("blocked", detail)

                    if should_wait and not self._headless:
                        if not self._wait_for_block_resolution(page, job_id, block):
                            return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)
                    else:
                        return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)

                # Locate fields; if none found, check for an "Apply" button to open the form
                fields = extract_form_fields(page)
                if len(fields) == 0:
                    apply_btn = page.locator(
                        "a:has-text('Apply for this job'), button:has-text('Apply for this job'), "
                        "a:has-text('Apply Now'), button:has-text('Apply Now'), "
                        "a:has-text('Apply'), button:has-text('Apply')"
                    ).first
                    if apply_btn.count() > 0 and apply_btn.is_visible():
                        self._log("Found 'Apply' button on job page. Clicking to navigate to application form...")
                        self._hands.click_element(page, apply_btn, label="Clicking Apply...")
                        page.wait_for_timeout(3000)
                        self._hands.inject_cursor(page)
                        fields = extract_form_fields(page)
                        self._log(f"Navigated to application form: {page.url} ({len(fields)} fields detected)")
                        self._storage.add_journey_step(
                            job_id=job_id,
                            step="navigation",
                            agent="BrowserApplicant",
                            status="info",
                            summary="Clicked Apply button to navigate to form",
                            details={"form_url": page.url, "fields_count": len(fields)},
                        )

                if len(fields) == 0:
                    detail = "No application form fields found on page (may require external login or portal)."
                    self._log(f"BLOCKED: {detail}")
                    screenshot = self._capture_screenshot(page, job_id, "no_form")
                    block = self._storage.block_job(
                        job_id=job_id,
                        reason="unsupported_portal",
                        detail=detail,
                        page_url=page.url,
                        screenshot_path=screenshot,
                    )
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="block_detected",
                        agent="BrowserApplicant",
                        status="blocked",
                        summary=detail,
                        details={"reason": "unsupported_portal", "screenshot": screenshot},
                    )
                    if self._notifier:
                        self._notifier.notify_block(block)
                    self._status("blocked", detail)

                    if should_wait and not self._headless:
                        if self._wait_for_block_resolution(page, job_id, block):
                            fields = extract_form_fields(page)
                        else:
                            return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)
                    else:
                        self._keep_browser_open_for_inspection(page, seconds=15)
                        return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)

                self._log(f"Detected {len(fields)} interactive form fields.")

                for field in fields:
                    prompt = normalize_field_prompt(field)
                    if not prompt:
                        continue

                    try:
                        # First: Check if the user already filled this field directly into Chrome!
                        existing_val = self._read_element_value(page, field)
                        if existing_val:
                            self._log(f"Field '{field.label or prompt}' already filled: {existing_val[:30]}")
                            self._save_learned(prompt, existing_val, job_id=job_id)
                            self._storage.add_journey_step(
                                job_id=job_id,
                                step="field_input",
                                agent="BrowserApplicant",
                                status="success",
                                summary=f"User already filled '{field.label or prompt}': '{existing_val}'",
                                details={"field": prompt, "value": existing_val, "type": field.input_type},
                            )
                            continue

                        # 1. File upload (Resume)
                        if field.input_type == "file":
                            resume_path = self._find_resume_pdf()
                            if resume_path:
                                self._log(f"Uploading resume: {resume_path.name}")
                                page.locator(field.selector).first.set_input_files(str(resume_path))
                                page.wait_for_timeout(2000)
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="file_upload",
                                    agent="BrowserApplicant",
                                    status="success",
                                    summary=f"Attached resume PDF: {resume_path.name}",
                                    details={"path": str(resume_path), "field": prompt},
                                )
                            elif field.is_required:
                                detail = "Application requires a resume PDF, but none is uploaded in profile/resumes/"
                                self._log(f"BLOCKED: {detail}")
                                screenshot = self._capture_screenshot(page, job_id, "no_resume")
                                block = self._storage.block_job(
                                    job_id=job_id,
                                    reason="other",
                                    detail=detail,
                                    page_url=page.url,
                                    screenshot_path=screenshot,
                                )
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="block_detected",
                                    agent="BrowserApplicant",
                                    status="blocked",
                                    summary=detail,
                                    details={"reason": "missing_resume", "screenshot": screenshot},
                                )
                                if self._notifier:
                                    self._notifier.notify_block(block)
                                self._status("blocked", detail)
                                if should_wait and not self._headless:
                                    if not self._wait_for_block_resolution(page, job_id, block):
                                        return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)
                                else:
                                    self._keep_browser_open_for_inspection(page, seconds=20)
                                    return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)
                            continue

                        # 2. Radio Button Group
                        if field.input_type == "radio":
                            res = self._resolver.resolve(prompt)
                            chosen_opt = None
                            if res.answer:
                                chosen_opt = find_best_select_option(field.options, res.answer)

                            # If required and no direct answer, try graceful decline/prefer not to disclose option
                            if not chosen_opt and field.is_required:
                                for fallback_label in ("Prefer not to disclose", "Decline to state", "Decline", "Not Applicable", "Others"):
                                    chosen_opt = find_best_select_option(field.options, fallback_label)
                                    if chosen_opt:
                                        break

                            if chosen_opt:
                                self._log(f"Selecting radio for '{field.label or prompt}': {chosen_opt}")
                                r_loc = page.locator(f"input[type='radio'][name='{field.name}'][value='{chosen_opt}']").first
                                target_loc = r_loc if r_loc.count() > 0 else page.locator(f"label:has-text('{chosen_opt}')").first
                                self._hands.click_element(page, target_loc, label=f"Select: {chosen_opt}")
                                page.wait_for_timeout(300)
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="field_input",
                                    agent="BrowserApplicant",
                                    status="success",
                                    summary=f"Selected radio '{field.label or prompt}': {chosen_opt}",
                                    details={"field": prompt, "value": chosen_opt, "type": "radio"},
                                )
                            elif field.is_required and (res.blocked_reason or (res.needs_review and not res.answer)):
                                if not should_wait or self._headless:
                                    return self._handle_blocked_question(page, job_id, prompt, should_wait=False)
                                resolved_val = self._wait_for_question_resolution(page, job_id, field, prompt)
                                if resolved_val is None:
                                    return ApplicantOutcome(status="blocked", detail=f"Blocked on required question: {prompt}", job_id=job_id)
                            continue

                        # 3. Checkbox
                        if field.input_type == "checkbox":
                            res = self._resolver.resolve(prompt)
                            if res.answer and res.answer.lower() in ("yes", "true", "agree", "accept", "1"):
                                self._log(f"Checking box '{field.label or prompt}'")
                                cb_loc = page.locator(field.selector).first
                                self._hands.click_element(page, cb_loc, label="Checking box")
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="field_input",
                                    agent="BrowserApplicant",
                                    status="success",
                                    summary=f"Checked box '{field.label or prompt}'",
                                    details={"field": prompt, "value": "checked", "type": "checkbox"},
                                )
                            continue

                        # 4. Dropdown / Select
                        if field.tag == "select":
                            res = self._resolver.resolve(prompt)
                            if res.answer:
                                opt = find_best_select_option(field.options, res.answer)
                                if opt:
                                    self._log(f"Selecting dropdown '{field.label or prompt}': {opt}")
                                    page.locator(field.selector).first.select_option(label=opt)
                                    page.wait_for_timeout(300)
                                    self._storage.add_journey_step(
                                        job_id=job_id,
                                        step="field_input",
                                        agent="BrowserApplicant",
                                        status="success",
                                        summary=f"Selected dropdown '{field.label or prompt}': {opt}",
                                        details={"field": prompt, "value": opt, "type": "select"},
                                    )
                            elif field.is_required and (res.blocked_reason or (res.needs_review and not res.answer)):
                                if not should_wait or self._headless:
                                    return self._handle_blocked_question(page, job_id, prompt, should_wait=False)
                                resolved_val = self._wait_for_question_resolution(page, job_id, field, prompt)
                                if resolved_val is None:
                                    return ApplicantOutcome(status="blocked", detail=f"Blocked on required question: {prompt}", job_id=job_id)
                            continue

                        # 5. Text, Email, Tel, Textarea
                        res = self._resolver.resolve(prompt)
                        if res.answer:
                            self._log(f"Filled '{field.label or prompt}': {res.answer[:30]}...")
                            loc = page.locator(field.selector).first
                            self._hands.type_into_element(page, loc, res.answer, label=f"Typing {prompt[:12]}...")
                            page.wait_for_timeout(200 if not self._headless else 50)
                            self._storage.add_journey_step(
                                job_id=job_id,
                                step="field_input",
                                agent="BrowserApplicant",
                                status="success",
                                summary=f"Filled field '{field.label or prompt}'",
                                details={"field": prompt, "value": res.answer[:50], "source": res.source, "type": field.input_type},
                            )
                        elif field.is_required and (res.blocked_reason or (res.needs_review and not res.answer)):
                            if not should_wait or self._headless:
                                return self._handle_blocked_question(page, job_id, prompt, should_wait=False)
                            resolved_val = self._wait_for_question_resolution(page, job_id, field, prompt)
                            if resolved_val is None:
                                return ApplicantOutcome(status="blocked", detail=f"Blocked on required question: {prompt}", job_id=job_id)

                    except Exception as field_err:
                        self._log(f"Warning: Could not fill field '{prompt}': {field_err}")
                        continue

                # Submission handling
                if dry_run:
                    detail = "Dry-run mode: Form filled successfully, paused before final submit."
                    self._log(detail)
                    self._status("ready_to_submit", detail)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="submission",
                        agent="BrowserApplicant",
                        status="success",
                        summary="Dry-run completed: Form filled successfully, ready for submit.",
                        details={"dry_run": True},
                    )
                    self._keep_browser_open_for_inspection(page, seconds=30)
                    return ApplicantOutcome(status="ready_to_submit", detail=detail, job_id=job_id)

                # Click Submit
                self._log("Submitting application...")
                submit_btn = page.locator("button[type='submit'], input[type='submit'], button:has-text('Submit'), button:has-text('Apply')").first
                if submit_btn.is_visible():
                    self._hands.click_element(page, submit_btn, label="Submitting Application...")
                    page.wait_for_timeout(3000)
                    self._storage.set_status(job_id, "applied")
                    detail = "Application submitted successfully."
                    self._log(detail)
                    self._status("applied", detail)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="submission",
                        agent="BrowserApplicant",
                        status="success",
                        summary=detail,
                        details={"dry_run": False},
                    )
                    self._keep_browser_open_for_inspection(page, seconds=10)
                    return ApplicantOutcome(status="applied", detail=detail, job_id=job_id)
                else:
                    detail = "Form filled, but could not locate submit button."
                    self._log(detail)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="submission",
                        agent="BrowserApplicant",
                        status="warning",
                        summary=detail,
                    )
                    self._keep_browser_open_for_inspection(page, seconds=15)
                    return ApplicantOutcome(status="ready_to_submit", detail=detail, job_id=job_id)

            except Exception as exc:
                detail = f"Error during application: {exc}"
                self._log(detail)
                self._status("failed", detail)
                self._storage.add_journey_step(
                    job_id=job_id,
                    step="submission",
                    agent="BrowserApplicant",
                    status="failed",
                    summary=detail,
                )
                self._keep_browser_open_for_inspection(page, seconds=20)
                return ApplicantOutcome(status="failed", detail=detail, job_id=job_id)
            finally:
                browser.close()

    def _keep_browser_open_for_inspection(self, page, seconds: int = 30) -> None:
        if self._headless:
            return
        self._log(f"Browser will stay open for {seconds}s for your inspection (or close the window when done)...")
        try:
            for _ in range(seconds):
                if page.is_closed():
                    break
                page.wait_for_timeout(1000)
        except Exception:
            pass

    def _detect_captcha(self, page) -> bool:
        indicators = [
            "iframe[src*='challenges.cloudflare.com']",
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            ".cf-turnstile",
            "#challenge-stage",
        ]
        try:
            for sel in indicators:
                if page.locator(sel).count() > 0:
                    return True
        except Exception:
            return False
        return False

    def _handle_blocked_question(self, page, job_id: int, prompt: str, should_wait: bool = True) -> ApplicantOutcome:
        screenshot = self._capture_screenshot(page, job_id, "question")
        detail = f"Blocked on required question needing input: {prompt}"
        self._log(f"BLOCKED: {detail}")
        page_url = page.url if hasattr(page, "url") else None
        block = self._storage.block_job(
            job_id=job_id,
            reason="unknown_question",
            detail=detail,
            page_url=page_url,
            screenshot_path=screenshot,
        )
        self._storage.add_journey_step(
            job_id=job_id,
            step="block_detected",
            agent="BrowserApplicant",
            status="blocked",
            summary=detail,
            details={"question": prompt, "screenshot": screenshot, "reason": "unknown_question"},
        )
        if self._notifier:
            self._notifier.notify_block(block)
        self._status("blocked", detail)

        if should_wait and not self._headless:
            if self._wait_for_block_resolution(page, job_id, block):
                return ApplicantOutcome(status="ready_to_submit", detail="Resumed after block resolution", job_id=job_id)

        self._keep_browser_open_for_inspection(page, seconds=45)
        return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)
