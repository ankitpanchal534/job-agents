from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest
from fakes import FakeChannel

from jobagent.applicant import Applicant
from jobagent.notify import Notifier
from jobagent.resolver import Resolution
from jobagent.storage import Storage



HTML_FORM = """<!DOCTYPE html>
<html>
<body>
  <form id="app-form">
    <label for="name">Full Name</label>
    <input id="name" name="name" type="text" required>

    <label for="email">Email Address</label>
    <input id="email" name="email" type="email" required>

    <label for="why">Why do you want to work here?</label>
    <textarea id="why" name="why"></textarea>

    <label for="auth">Are you authorized to work in the US?</label>
    <select id="auth" name="auth">
      <option value="">Select...</option>
      <option value="yes">Yes</option>
      <option value="no">No</option>
    </select>

    <label for="resume">Attach Resume</label>
    <input id="resume" name="resume" type="file">

    <button type="submit" id="submit-btn">Submit Application</button>
  </form>
</body>
</html>
"""

HTML_CAPTCHA = """<!DOCTYPE html>
<html>
<body>
  <div class="cf-turnstile">Cloudflare verification</div>
</body>
</html>
"""

HTML_BLOCKED_QUESTION = """<!DOCTYPE html>
<html>
<body>
  <form>
    <label for="salary">What is your expected salary?</label>
    <input id="salary" name="salary" type="text" required>
  </form>
</body>
</html>
"""

HTML_FORM_SUCCESS = """<!DOCTYPE html>
<html>
<body>
  <form id="app-form" action="/confirmation" method="get">
    <label for="name">Full Name</label>
    <input id="name" name="name" type="text" required>

    <label for="email">Email Address</label>
    <input id="email" name="email" type="email" required>

    <button type="submit" id="submit-btn">Submit Application</button>
  </form>
</body>
</html>
"""

HTML_CONFIRMATION = """<!DOCTYPE html>
<html>
<body>
  <div id="application_confirmation">
    <h2>Thank you for applying!</h2>
    <p>Your application has been received and is under review.</p>
  </div>
</body>
</html>
"""

HTML_FORM_ERROR = """<!DOCTYPE html>
<html>
<body>
  <form id="app-form" onsubmit="event.preventDefault(); document.getElementById('err').style.display='block';">
    <label for="name">Full Name</label>
    <input id="name" name="name" type="text" required>

    <div id="err" class="error-message" style="display:none;">This field is required: Phone Number is missing</div>
    <button type="submit" id="submit-btn">Submit Application</button>
  </form>
</body>
</html>
"""

HTML_FORM_UNCONFIRMED = """<!DOCTYPE html>
<html>
<body>
  <form id="app-form" onsubmit="event.preventDefault();">
    <label for="name">Full Name</label>
    <input id="name" name="name" type="text" required>

    <button type="submit" id="submit-btn">Submit Application</button>
  </form>
</body>
</html>
"""


HTML_CUSTOM_DROPDOWN_AND_SCROLL = """<!DOCTYPE html>
<html>
<body style="min-height: 2000px; margin: 0; padding: 20px;">
  <div style="height: 600px; background: #eee;">
    <h1>Job Description</h1>
    <p>Scroll down or click Apply to reach application form.</p>
    <button id="apply-btn" onclick="document.getElementById('form-section').scrollIntoView();">Apply for this job</button>
  </div>
  <div id="form-section" style="margin-top: 50px;">
    <form id="app-form">
      <label for="name">Full Name</label>
      <input id="name" name="name" type="text" required>

      <label for="custom-select">Experience Level</label>
      <div id="custom-select" role="combobox" class="select__control" style="width: 200px; height: 35px; border: 1px solid black;" onclick="document.getElementById('options-list').style.display = 'block';">
        <span id="selected-val">Choose...</span>
      </div>
      <div id="options-list" style="display: none;">
        <div role="option" class="select__option" onclick="document.getElementById('selected-val').innerText='Senior (5+ years)'; document.getElementById('options-list').style.display='none';">Senior (5+ years)</div>
        <div role="option" class="select__option" onclick="document.getElementById('selected-val').innerText='Junior (1-3 years)'; document.getElementById('options-list').style.display='none';">Junior (1-3 years)</div>
      </div>

      <button type="submit" id="submit-app">Submit Application</button>
    </form>
  </div>
</body>
</html>
"""


class MockHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.path == "/captcha":
            self.wfile.write(HTML_CAPTCHA.encode("utf-8"))
        elif self.path == "/blocked":
            self.wfile.write(HTML_BLOCKED_QUESTION.encode("utf-8"))
        elif self.path.startswith("/form_success"):
            self.wfile.write(HTML_FORM_SUCCESS.encode("utf-8"))
        elif self.path.startswith("/confirmation"):
            self.wfile.write(HTML_CONFIRMATION.encode("utf-8"))
        elif self.path.startswith("/form_error"):
            self.wfile.write(HTML_FORM_ERROR.encode("utf-8"))
        elif self.path.startswith("/form_unconfirmed"):
            self.wfile.write(HTML_FORM_UNCONFIRMED.encode("utf-8"))
        elif self.path.startswith("/custom_dropdown"):
            self.wfile.write(HTML_CUSTOM_DROPDOWN_AND_SCROLL.encode("utf-8"))
        else:
            self.wfile.write(HTML_FORM.encode("utf-8"))


    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def mock_server():
    server = socketserver.TCPServer(("127.0.0.1", 0), MockHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


class FakeResolver:
    def resolve(self, question: str, max_length=None, jd_summary="") -> Resolution:
        q = question.lower()
        if "name" in q:
            return Resolution(answer="Jane Doe", source="curated")
        if "email" in q:
            return Resolution(answer="jane@example.com", source="curated")
        if "why" in q:
            return Resolution(answer="Excited about the role.", source="generated")
        if "authorized" in q:
            return Resolution(answer="Yes", source="curated")
        if "experience" in q:
            return Resolution(answer="Senior", source="curated")
        if "salary" in q:
            return Resolution(answer=None, blocked_reason="needs_input")
        return Resolution(answer=None, blocked_reason="needs_input")


def test_applicant_dry_run_fills_form_and_resumes(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(mock_server)

    # Create dummy resume
    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    logs = []
    statuses = []

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        on_log=lambda m: logs.append(m),
        on_status=lambda s, d: statuses.append((s, d)),
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)

    assert outcome.status == "ready_to_submit"
    assert any("Filled" in l for l in logs)
    assert any("Jane Doe" in l for l in logs)
    assert any("Uploading resume" in l for l in logs)


def test_applicant_blocks_on_captcha(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(f"{mock_server}/captcha")

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)
    assert outcome.status == "blocked"
    assert "CAPTCHA" in outcome.detail
    blocks = storage.list_blocks()
    assert len(blocks) == 1
    assert blocks[0].reason == "captcha"


def test_applicant_blocks_on_sensitive_question(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(f"{mock_server}/blocked")

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)
    assert outcome.status == "blocked"
    assert "salary" in outcome.detail.lower()
    blocks = storage.list_blocks()
    assert len(blocks) == 1
    assert blocks[0].reason == "unknown_question"


def test_applicant_with_persistent_user_data_dir(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(mock_server)

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    user_profile = tmp_path / "chrome_user_data"
    user_profile.mkdir(parents=True)

    logs = []
    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        user_data_dir=user_profile,
        on_log=lambda m: logs.append(m),
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)
    assert outcome.status == "ready_to_submit"
    assert any("persistent user profile" in l.lower() for l in logs)
    assert any(user_profile.iterdir())


def test_applicant_cdp_fallback_to_persistent(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(mock_server)

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    user_profile = tmp_path / "chrome_user_data_fallback"
    user_profile.mkdir(parents=True)

    logs = []
    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        cdp_url="http://127.0.0.1:59999",
        user_data_dir=user_profile,
        on_log=lambda m: logs.append(m),
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)
    assert outcome.status == "ready_to_submit"
    assert any("failed to connect to chrome" in l.lower() for l in logs)
    assert any("falling back to persistent chrome profile" in l.lower() for l in logs)


def test_applicant_with_named_profile_directory(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(mock_server)

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    user_profile = tmp_path / "chrome_user_data_named"
    user_profile.mkdir(parents=True)

    logs = []
    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        user_data_dir=user_profile,
        profile_directory="jobagent",
        on_log=lambda m: logs.append(m),
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)
    assert outcome.status == "ready_to_submit"
    assert any("jobagent" in l for l in logs)
    subdirs = [p.name for p in user_profile.iterdir() if p.is_dir()]
    assert "jobagent" in subdirs


def test_applicant_verified_submission_success_and_notifies(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(f"{mock_server}/form_success")
    storage.update_job(job.id, title="Frontend Engineer", company="Stripe")

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    channel = FakeChannel()
    notifier = Notifier(storage=storage, channels=[channel])

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        notifier=notifier,
        headless=True,
        profile_dir=profile_dir,
        wait_for_human=False,
        submission_timeout=5.0,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=False, wait_for_human=False)

    assert outcome.status == "applied"
    assert "successfully" in outcome.detail.lower()

    # Verify database status updated
    updated_job = storage.get_job(job.id)
    assert updated_job.status == "applied"

    # Verify journey steps
    steps = storage.list_journey_steps(job.id)
    sub_steps = [s for s in steps if s.step == "submission"]
    assert len(sub_steps) > 0
    last_sub = sub_steps[-1]
    assert last_sub.status == "success"
    assert last_sub.details.get("screenshot")
    assert Path(last_sub.details["screenshot"]).exists()

    # Verify ntfy notification sent with screenshot and job link
    assert len(channel.sent) == 1
    alert = channel.sent[0]
    assert alert.title == "Job Applied"
    assert "Job Applied" in alert.message
    assert "Frontend Engineer" in alert.message
    assert "Stripe" in alert.message
    assert alert.url == f"{mock_server}/form_success"
    assert alert.attachment == last_sub.details["screenshot"]


def test_applicant_detects_submission_error_does_not_falsely_apply(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(f"{mock_server}/form_error")
    storage.update_job(job.id, title="Frontend Engineer", company="Stripe")

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    channel = FakeChannel()
    notifier = Notifier(storage=storage, channels=[channel])

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        notifier=notifier,
        headless=True,
        profile_dir=profile_dir,
        wait_for_human=False,
        submission_timeout=5.0,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=False, wait_for_human=False)

    assert outcome.status == "failed"
    assert "error" in outcome.detail.lower()

    # Crucial: status must NOT be "applied"
    updated_job = storage.get_job(job.id)
    assert updated_job.status != "applied"

    # Journey step must be failed with screenshot
    steps = storage.list_journey_steps(job.id)
    sub_steps = [s for s in steps if s.step == "submission"]
    assert len(sub_steps) > 0
    last_sub = sub_steps[-1]
    assert last_sub.status == "failed"
    assert last_sub.details.get("screenshot")
    assert Path(last_sub.details["screenshot"]).exists()
    assert "Phone Number is missing" in last_sub.details["error"]

    # Did not falsely send "Job Applied" notification
    applied_alerts = [a for a in channel.sent if a.title == "Job Applied"]
    assert len(applied_alerts) == 0


def test_applicant_detects_unconfirmed_submission_does_not_falsely_apply(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(f"{mock_server}/form_unconfirmed")
    storage.update_job(job.id, title="Frontend Engineer", company="Stripe")

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        wait_for_human=False,
        submission_timeout=2.0,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=False, wait_for_human=False)

    assert outcome.status != "applied"
    assert outcome.status == "ready_to_submit"

    # Crucial: status must NOT be "applied"
    updated_job = storage.get_job(job.id)
    assert updated_job.status != "applied"

    # Journey step warning with screenshot
    steps = storage.list_journey_steps(job.id)
    sub_steps = [s for s in steps if s.step == "submission"]
    assert len(sub_steps) > 0
    last_sub = sub_steps[-1]
    assert last_sub.status == "warning"
    assert last_sub.details.get("screenshot")
    assert Path(last_sub.details["screenshot"]).exists()


class FakeVisionLLM:
    def complete(self, task: str, system: str, user: str, image_path: str | None = None) -> str:
        if "experience" in user.lower() or "senior" in user.lower():
            return "Senior (5+ years)"
        return "Senior (5+ years)"


def test_applicant_visual_mode_scroll_and_custom_dropdown(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(f"{mock_server}/custom_dropdown")

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    fake_resume = resumes_dir / "resume.pdf"
    fake_resume.write_text("dummy resume content")

    fake_llm = FakeVisionLLM()
    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        wait_for_human=False,
        llm=fake_llm,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True, wait_for_human=False)
    assert outcome.status == "ready_to_submit"

    # Verify journey steps recorded field_input
    steps = storage.list_journey_steps(job.id)
    step_types = [s.step for s in steps]
    assert "field_input" in step_types


