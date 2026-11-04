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


def get_or_create_chrome_profile(
    user_data_dir: Optional[str | Path] = None,
    profile_name: str = "jobagent",
) -> tuple[Path, str]:
    """Ensures a dedicated Chrome profile named `profile_name` exists and is initialized."""
    import json
    import platform

    if not user_data_dir:
        system = platform.system()
        if system == "Darwin":
            user_data_dir = Path.home() / "Library" / "Application Support" / "JobAgent" / "Chrome"
        elif system == "Windows":
            user_data_dir = Path.home() / "AppData" / "Local" / "JobAgent" / "Chrome"
        else:
            user_data_dir = Path.home() / ".config" / "jobagent" / "chrome"
    else:
        user_data_dir = Path(user_data_dir).expanduser().resolve()

    user_data_dir.mkdir(parents=True, exist_ok=True)
    named_profile = user_data_dir / profile_name
    named_profile.mkdir(parents=True, exist_ok=True)

    # Initialize Local State if not present or missing profile
    local_state_file = user_data_dir / "Local State"
    local_state = {}
    if local_state_file.exists():
        try:
            local_state = json.loads(local_state_file.read_text(encoding="utf-8"))
        except Exception:
            local_state = {}

    local_state.setdefault("profile", {}).setdefault("info_cache", {})[profile_name] = {
        "name": profile_name,
        "is_using_default_name": False,
        "active_time": time.time(),
    }
    try:
        local_state_file.write_text(json.dumps(local_state, indent=2), encoding="utf-8")
    except Exception:
        pass

    # Initialize Preferences inside the profile folder
    pref_file = named_profile / "Preferences"
    pref = {}
    if pref_file.exists():
        try:
            pref = json.loads(pref_file.read_text(encoding="utf-8"))
        except Exception:
            pref = {}
    pref.setdefault("profile", {})["name"] = profile_name
    try:
        pref_file.write_text(json.dumps(pref, indent=2), encoding="utf-8")
    except Exception:
        pass

    return user_data_dir, profile_name


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
        on_resolve: Optional[Callable[[str, str, str], None]] = None,
        on_action: Optional[Callable[[str, str, str], None]] = None,
        user_data_dir: Optional[Path | str] = None,
        cdp_url: Optional[str] = None,
        profile_directory: Optional[str] = "jobagent",
        submission_timeout: float = 25.0,
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
        self._on_resolve = on_resolve or (lambda q, a, s: None)
        self._on_action = on_action or (lambda act, tgt, ext: None)
        self._user_data_dir = str(user_data_dir) if user_data_dir else None
        self._cdp_url = str(cdp_url) if cdp_url else None
        self._profile_directory = profile_directory
        self._submission_timeout = submission_timeout

    def _log(self, message: str) -> None:
        self._on_log(message)

    def _status(self, status: str, detail: str) -> None:
        self._on_status(status, detail)

    def _resolve_notify(self, question: str, answer: str, source: str) -> None:
        try:
            self._on_resolve(question, answer, source)
        except Exception:
            pass

    def _action_notify(self, action: str, target: str, extra: str = "") -> None:
        try:
            self._on_action(action, target, extra)
        except Exception:
            pass

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
        self._resolve_notify(prompt, "Needs input (blocked)", "needs_input")
        self._action_notify("Waiting for Human", prompt, "Chrome open & highlighted")
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
        self._resolve_notify(prompt, user_val, "Human in Chrome")
        self._action_notify("Answered", prompt, f"Captured: {user_val[:20]}")

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

            # 3. Check if submission succeeded while waiting on unconfirmed/error
            if block.reason == "submit_unconfirmed":
                res, res_msg = self._check_submission_response(page, block.page_url or "")
                if res == "success":
                    self._log(f"✓ Submission confirmation detected on screen ({res_msg})! Resuming...")
                    self._storage.resume_job(job_id)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="block_resolved",
                        agent="BrowserApplicant",
                        status="success",
                        summary=f"Submission confirmed in browser: {res_msg}",
                        details={"block_id": block.id},
                    )
                    return True

            page.wait_for_timeout(1000)

        self._log(f"Timed out after {timeout_seconds}s waiting for human resolution.")
        return False

    def _find_submit_button(self, page):
        """Locates the primary submit button on the application form, avoiding auth/social buttons."""
        submit_candidates = [
            "button[type='submit']",
            "input[type='submit']",
            "button[data-qa='btn-submit']",
            "#submit_app",
            "#submit-btn",
            "button:has-text('Submit application')",
            "button:has-text('Submit Application')",
            "button:has-text('Submit application now')",
            "button:has-text('Submit')",
            "input[value='Submit application']",
            "input[value='Submit Application']",
            "input[value='Submit']",
            "form button:has-text('Apply')",
        ]
        for sel in submit_candidates:
            try:
                locator = page.locator(sel)
                count = locator.count()
                for i in range(count):
                    el = locator.nth(i)
                    if el.is_visible():
                        txt = (el.inner_text() or el.get_attribute("value") or "").lower()
                        # Avoid external social sign-in buttons (e.g. "Apply with LinkedIn")
                        if any(auth in txt for auth in ("linkedin", "indeed", "google", "facebook", "seek")):
                            continue
                        return el
            except Exception:
                continue

        # Fallback: any visible button containing 'apply' or 'submit' inside a form
        try:
            form_btns = page.locator("form button, form input[type='button'], form a.btn, form a.button")
            for i in range(form_btns.count()):
                el = form_btns.nth(i)
                if el.is_visible():
                    txt = (el.inner_text() or el.get_attribute("value") or "").lower()
                    if ("submit" in txt or "apply" in txt) and not any(auth in txt for auth in ("linkedin", "indeed", "google")):
                        return el
        except Exception:
            pass

        return None

    def _check_submission_response(self, page, initial_url: str) -> tuple[str, str]:
        """Checks the page for submission outcome (success, error, pending, or unknown)."""
        try:
            current_url = page.url.lower()
            init_url_lower = initial_url.lower()

            # 1. URL navigation to confirmation page
            url_success_keywords = [
                "/confirmation",
                "/confirmed",
                "/thanks",
                "/thank-you",
                "/thank_you",
                "/submitted",
                "/success",
                "/complete",
                "/applied",
            ]
            if current_url != init_url_lower and any(kw in current_url for kw in url_success_keywords):
                return ("success", f"Page navigated to confirmation URL: {page.url}")

            # 2. Check for explicit success containers in DOM
            success_selectors = [
                "#application_confirmation",
                ".application-confirmation",
                ".submitted-message",
                ".submission-success",
                "[data-qa='msg-submit-success']",
                ".success-message",
                ".application-success",
            ]
            for sel in success_selectors:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible():
                        txt = loc.inner_text().strip()
                        return ("success", f"Confirmation container visible: {txt[:120]}")
                except Exception:
                    pass

            # 3. Check for visible success phrases in headings and content
            success_phrases = [
                "thank you for applying",
                "thanks for applying",
                "application submitted",
                "application has been submitted",
                "your application was submitted",
                "your application has been received",
                "we've received your application",
                "we have received your application",
                "application received",
                "submission received",
                "successfully submitted",
                "applied successfully",
                "thank you for your interest",
                "thanks for your interest",
                "application complete",
            ]
            found_success = page.evaluate("""(phrases) => {
                const candidates = Array.from(document.querySelectorAll('h1, h2, h3, h4, [role="alert"], [role="status"], .alert, p, div'));
                for (const el of candidates) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    
                    const text = (el.innerText || '').toLowerCase();
                    for (const phrase of phrases) {
                        if (text.includes(phrase)) {
                            return el.innerText.trim().slice(0, 150);
                        }
                    }
                }
                return null;
            }""", success_phrases)

            if found_success:
                return ("success", f"Success message found: {found_success}")

            # 4. Check for visible submission validation / error messages
            error_phrases = [
                "please fill in this field",
                "please fill out this field",
                "this field is required",
                "is required",
                "invalid email",
                "please enter a valid",
                "please fix the errors",
                "error submitting application",
                "there was an error submitting",
                "there was a problem submitting",
                "submission failed",
                "something went wrong",
                "could not submit",
            ]
            found_errors = page.evaluate("""(phrases) => {
                const errors = [];
                // Selector-based error containers
                const errorEls = Array.from(document.querySelectorAll(
                    '.error-message, .error_message, .error-summary, .alert-danger, .flash-error, ' +
                    '.toast-error, .form-error, .field-error, .input--error, .validation-error, ' +
                    '[aria-invalid="true"], [role="alert"].error, span.error, div.error, p.error, .has-error'
                ));
                for (const el of errorEls) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const txt = (el.innerText || '').trim();
                    if (txt && !errors.includes(txt) && txt.length < 200) {
                        errors.push(txt);
                    }
                }
                
                // Visible phrase matching
                if (errors.length === 0) {
                    const textEls = Array.from(document.querySelectorAll('p, span, div, label, [role="alert"]'));
                    for (const el of textEls) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width === 0 || rect.height === 0) continue;
                        const style = window.getComputedStyle(el);
                        if (style.display === 'none' || style.visibility === 'hidden') continue;
                        const txt = (el.innerText || '').toLowerCase();
                        for (const phrase of phrases) {
                            if (txt.includes(phrase) && el.innerText.trim().length < 150) {
                                const originalTxt = el.innerText.trim();
                                if (!errors.includes(originalTxt)) {
                                    errors.push(originalTxt);
                                }
                            }
                        }
                    }
                }
                return errors;
            }""", error_phrases)

            if found_errors:
                return ("error", "; ".join(found_errors))

            # 5. Check if button is in loading / submitting state
            is_submitting = page.evaluate("""() => {
                const submitBtn = document.querySelector('button[type="submit"], input[type="submit"], button.submitting, [data-qa="btn-submit"]');
                if (submitBtn) {
                    if (submitBtn.disabled || submitBtn.getAttribute('aria-disabled') === 'true') return true;
                    if (submitBtn.classList.contains('loading') || submitBtn.classList.contains('submitting') || submitBtn.classList.contains('spinner')) return true;
                }
                const spinners = document.querySelectorAll('.spinner, .loading-spinner, [role="progressbar"]');
                for (const s of spinners) {
                    const rect = s.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) return true;
                }
                return false;
            }""")
            if is_submitting:
                return ("pending", "Submission request in progress...")

            return ("unknown", "Awaiting response...")
        except Exception as exc:
            return ("unknown", f"Error checking response: {exc}")

    def _wait_for_submission_response(self, page, initial_url: str, timeout_seconds: float = 25.0) -> tuple[str, str]:
        """Polls the page after submit button click to verify webpage response (success/error/captcha/timeout)."""
        start_poll = time.time()
        last_status = "unknown"
        last_msg = "No response from submission"

        while time.time() - start_poll < timeout_seconds:
            if page.is_closed():
                return ("error", "Browser window was closed before submission completed.")

            if self._detect_captcha(page):
                return ("captcha", "CAPTCHA challenge detected upon submission")

            st_res, st_msg = self._check_submission_response(page, initial_url)
            last_status = st_res
            last_msg = st_msg

            if st_res in ("success", "error"):
                return (st_res, st_msg)

            page.wait_for_timeout(1000)

        # Timeout reached: if still unknown, verify if form is still present
        try:
            form_present = page.locator("form, button[type='submit'], input[type='submit']").first.is_visible()
            if form_present:
                return ("unconfirmed", "Form is still present on page; submission was not confirmed by the server.")
        except Exception:
            pass

        return (last_status, last_msg)


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
            browser = None
            context = None
            is_cdp = False

            if self._cdp_url:
                try:
                    self._log(f"Connecting to running Chrome instance via CDP ({self._cdp_url})...")
                    browser = p.chromium.connect_over_cdp(self._cdp_url)
                    context = browser.contexts[0] if browser.contexts else browser.new_context()
                    page = context.new_page()
                    is_cdp = True
                except Exception as cdp_err:
                    self._log(f"Failed to connect to Chrome at {self._cdp_url}: {cdp_err}")
                    if self._user_data_dir:
                        self._log(f"Falling back to persistent Chrome profile at {self._user_data_dir}...")
                    else:
                        raise

            if not is_cdp:
                if not self._headless or self._user_data_dir:
                    pdir, pname = get_or_create_chrome_profile(
                        user_data_dir=self._user_data_dir,
                        profile_name=self._profile_directory or "jobagent",
                    )
                    extra_args = []
                    if not self._headless:
                        extra_args.append("--start-maximized")
                    extra_args.append(f"--profile-directory={pname}")

                    launch_kwargs = {
                        "user_data_dir": str(pdir),
                        "channel": "chrome",
                        "headless": self._headless,
                        "args": extra_args,
                    }
                    self._log(f"Opening Chrome with persistent user profile '{pname}' (storage: {pdir})...")
                    context = p.chromium.launch_persistent_context(**launch_kwargs)
                    page = context.pages[0] if context.pages else context.new_page()
                else:
                    extra_args = []
                    if self._profile_directory:
                        extra_args.append(f"--profile-directory={self._profile_directory}")
                    launch_kwargs = {"channel": "chrome", "headless": self._headless}
                    if extra_args:
                        launch_kwargs["args"] = extra_args
                    browser = p.chromium.launch(**launch_kwargs)
                    context = browser.new_context(viewport={"width": 1280, "height": 800} if self._headless else None)
                    page = context.new_page()

            try:
                self._log(f"Navigating to {url}...")
                self._action_notify("Navigating", url, "Opening job page in Chrome")
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
                        self._action_notify("Clicking", "Apply Button", "Navigating to application form")
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
                            self._resolve_notify(prompt, existing_val, "pre_filled")
                            self._action_notify("Verified", field.label or prompt, f"Pre-filled: {existing_val[:20]}")
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
                                self._action_notify("Attaching", "Resume PDF", resume_path.name)
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
                                self._resolve_notify(prompt, chosen_opt, res.source)
                                self._action_notify("Selecting", field.label or prompt, f"Radio: {chosen_opt}")
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
                                self._resolve_notify(prompt, res.answer, res.source)
                                self._action_notify("Checking", field.label or prompt, "Checkbox")
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
                                    self._resolve_notify(prompt, opt, res.source)
                                    self._action_notify("Selecting", field.label or prompt, f"Dropdown: {opt}")
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
                            self._resolve_notify(prompt, res.answer, res.source)
                            self._action_notify("Typing", field.label or prompt, f"Value: {res.answer[:20]}...")
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
                    self._action_notify("Completed", "Dry-Run Ready", "All fields filled successfully")
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

                # -------------------------------------------------------------
                # Submit Application & Verified Webpage Response
                # -------------------------------------------------------------
                self._log("Locating submit button...")
                submit_btn = self._find_submit_button(page)
                if not submit_btn:
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

                self._action_notify("Submitting", "Submit Button", "Clicking submit...")
                self._log("Submitting application...")
                initial_url = page.url

                try:
                    submit_btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass

                self._hands.click_element(page, submit_btn, label="Submitting Application...")
                self._log("Waiting for webpage response (success/error verification)...")

                sub_status, sub_msg = self._wait_for_submission_response(page, initial_url, timeout_seconds=self._submission_timeout)

                if sub_status == "success":
                    screenshot_path = self._capture_screenshot(page, job_id, "submission_success")
                    detail = f"Application submitted successfully: {sub_msg}"
                    self._log(f"✓ {detail}")
                    self._storage.set_status(job_id, "applied")
                    self._status("applied", detail)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="submission",
                        agent="BrowserApplicant",
                        status="success",
                        summary=detail,
                        details={
                            "dry_run": False,
                            "confirmation": sub_msg,
                            "url": page.url,
                            "screenshot": screenshot_path,
                        },
                    )

                    if self._notifier:
                        try:
                            self._log("Sending 'Job Applied' alert via ntfy with screenshot and job link...")
                            notif_report = self._notifier.notify_applied(
                                job_id=job_id,
                                screenshot_path=screenshot_path,
                                custom_message=f"Job Applied: {job.title or 'Application'} at {job.company or 'Company'}\nURL: {job.url}",
                            )
                            if notif_report.sent:
                                self._log(f"✓ Notification sent via {', '.join(notif_report.sent)}.")
                            elif notif_report.failed:
                                self._log(f"Warning: Notification failed on {notif_report.failed}")
                        except Exception as notif_err:
                            self._log(f"Warning: Could not send notification: {notif_err}")

                    self._keep_browser_open_for_inspection(page, seconds=10)
                    return ApplicantOutcome(status="applied", detail=detail, job_id=job_id)

                elif sub_status == "error":
                    screenshot_path = self._capture_screenshot(page, job_id, "submission_error")
                    detail = f"Submission error on page: {sub_msg}"
                    self._log(f"❌ {detail}")
                    self._status("failed", detail)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="submission",
                        agent="BrowserApplicant",
                        status="failed",
                        summary=detail,
                        details={
                            "dry_run": False,
                            "error": sub_msg,
                            "url": page.url,
                            "screenshot": screenshot_path,
                        },
                    )

                    if should_wait and not self._headless:
                        block = self._storage.block_job(
                            job_id=job_id,
                            reason="submit_unconfirmed",
                            detail=detail,
                            page_url=page.url,
                            screenshot_path=screenshot_path,
                        )
                        if self._notifier:
                            self._notifier.notify_block(block)
                        resolved = self._wait_for_block_resolution(page, job_id, block)
                        if resolved:
                            st_res, st_msg = self._check_submission_response(page, initial_url)
                            if st_res == "success":
                                succ_shot = self._capture_screenshot(page, job_id, "submission_success")
                                s_detail = f"Application submitted after human resolution: {st_msg}"
                                self._storage.set_status(job_id, "applied")
                                self._status("applied", s_detail)
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="submission",
                                    agent="BrowserApplicant",
                                    status="success",
                                    summary=s_detail,
                                    details={"url": page.url, "screenshot": succ_shot},
                                )
                                if self._notifier:
                                    self._notifier.notify_applied(
                                        job_id=job_id,
                                        screenshot_path=succ_shot,
                                        custom_message=f"Job Applied: {job.title or 'Application'} at {job.company or 'Company'}\nURL: {job.url}",
                                    )
                                self._keep_browser_open_for_inspection(page, seconds=10)
                                return ApplicantOutcome(status="applied", detail=s_detail, job_id=job_id)

                    return ApplicantOutcome(status="failed", detail=detail, job_id=job_id)

                elif sub_status == "captcha":
                    screenshot_path = self._capture_screenshot(page, job_id, "submission_captcha")
                    detail = "Submission blocked by CAPTCHA challenge"
                    self._log(f"BLOCKED: {detail}")
                    self._status("blocked", detail)
                    block = self._storage.block_job(
                        job_id=job_id,
                        reason="captcha",
                        detail=detail,
                        page_url=page.url,
                        screenshot_path=screenshot_path,
                    )
                    if self._notifier:
                        self._notifier.notify_block(block)

                    if should_wait and not self._headless:
                        resolved = self._wait_for_block_resolution(page, job_id, block)
                        if resolved:
                            st_res, st_msg = self._check_submission_response(page, initial_url)
                            if st_res == "success":
                                succ_shot = self._capture_screenshot(page, job_id, "submission_success")
                                s_detail = f"Application submitted after solving CAPTCHA: {st_msg}"
                                self._storage.set_status(job_id, "applied")
                                self._status("applied", s_detail)
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="submission",
                                    agent="BrowserApplicant",
                                    status="success",
                                    summary=s_detail,
                                    details={"url": page.url, "screenshot": succ_shot},
                                )
                                if self._notifier:
                                    self._notifier.notify_applied(
                                        job_id=job_id,
                                        screenshot_path=succ_shot,
                                        custom_message=f"Job Applied: {job.title or 'Application'} at {job.company or 'Company'}\nURL: {job.url}",
                                    )
                                self._keep_browser_open_for_inspection(page, seconds=10)
                                return ApplicantOutcome(status="applied", detail=s_detail, job_id=job_id)
                    return ApplicantOutcome(status="blocked", detail=detail, job_id=job_id)

                else:  # unconfirmed / timeout
                    screenshot_path = self._capture_screenshot(page, job_id, "submission_unconfirmed")
                    detail = f"Submission could not be confirmed: {sub_msg}"
                    self._log(f"⚠️ {detail}")
                    self._status("ready_to_submit", detail)
                    self._storage.add_journey_step(
                        job_id=job_id,
                        step="submission",
                        agent="BrowserApplicant",
                        status="warning",
                        summary=detail,
                        details={
                            "dry_run": False,
                            "unconfirmed_reason": sub_msg,
                            "url": page.url,
                            "screenshot": screenshot_path,
                        },
                    )

                    if should_wait and not self._headless:
                        block = self._storage.block_job(
                            job_id=job_id,
                            reason="submit_unconfirmed",
                            detail=detail,
                            page_url=page.url,
                            screenshot_path=screenshot_path,
                        )
                        if self._notifier:
                            self._notifier.notify_block(block)
                        resolved = self._wait_for_block_resolution(page, job_id, block)
                        if resolved:
                            st_res, st_msg = self._check_submission_response(page, initial_url)
                            if st_res == "success":
                                succ_shot = self._capture_screenshot(page, job_id, "submission_success")
                                s_detail = f"Application submitted after human confirmation: {st_msg}"
                                self._storage.set_status(job_id, "applied")
                                self._status("applied", s_detail)
                                self._storage.add_journey_step(
                                    job_id=job_id,
                                    step="submission",
                                    agent="BrowserApplicant",
                                    status="success",
                                    summary=s_detail,
                                    details={"url": page.url, "screenshot": succ_shot},
                                )
                                if self._notifier:
                                    self._notifier.notify_applied(
                                        job_id=job_id,
                                        screenshot_path=succ_shot,
                                        custom_message=f"Job Applied: {job.title or 'Application'} at {job.company or 'Company'}\nURL: {job.url}",
                                    )
                                self._keep_browser_open_for_inspection(page, seconds=10)
                                return ApplicantOutcome(status="applied", detail=s_detail, job_id=job_id)

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
                if is_cdp:
                    try:
                        if not page.is_closed():
                            page.close()
                    except Exception:
                        pass
                    if browser:
                        try:
                            browser.close()
                        except Exception:
                            pass
                elif context:
                    try:
                        context.close()
                    except Exception:
                        pass
                elif browser:
                    try:
                        browser.close()
                    except Exception:
                        pass

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
