from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path

import pytest

from jobagent.applicant import Applicant
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


class MockHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if self.path == "/captcha":
            self.wfile.write(HTML_CAPTCHA.encode("utf-8"))
        elif self.path == "/blocked":
            self.wfile.write(HTML_BLOCKED_QUESTION.encode("utf-8"))
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
