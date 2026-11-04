from __future__ import annotations

import http.server
import socketserver
import threading
from pathlib import Path
import pytest

from jobagent.applicant import Applicant
from jobagent.browser_hands import BrowserHands
from jobagent.resolver import Resolution
from jobagent.storage import Storage

HTML_PAGE = """<!DOCTYPE html>
<html>
<body>
  <form id="app-form">
    <label for="name">Full Name</label>
    <input id="name" name="name" type="text" required>

    <label for="email">Email Address</label>
    <input id="email" name="email" type="email" required>

    <label for="resume">Attach Resume</label>
    <input id="resume" name="resume" type="file">

    <button type="submit" id="submit-btn">Submit</button>
  </form>
</body>
</html>
"""

class MockHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_PAGE.encode("utf-8"))

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
            return Resolution(answer="Alice Smith", source="curated")
        if "email" in q:
            return Resolution(answer="alice@example.com", source="curated")
        return Resolution(answer=None, blocked_reason="needs_input")

def test_applicant_records_journey_steps(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(mock_server)

    profile_dir = tmp_path / "profile"
    resumes_dir = profile_dir / "resumes"
    resumes_dir.mkdir(parents=True)
    (resumes_dir / "resume.pdf").write_text("fake pdf")

    hands = BrowserHands(enabled=False)
    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        profile_dir=profile_dir,
        hands=hands,
        wait_for_human=False,
    )

    outcome = applicant.apply(job_id=job.id, dry_run=True)
    assert outcome.status == "ready_to_submit"

    steps = storage.list_journey_steps(job.id)
    assert len(steps) >= 3

    step_names = [s.step for s in steps]
    assert "navigation" in step_names
    assert "file_upload" in step_names
    assert "field_input" in step_names
    assert "submission" in step_names


def test_applicant_wait_for_block_resolution(mock_server, tmp_path):
    db_file = tmp_path / "test_jobs.db"
    storage = Storage(str(db_file))
    job = storage.add_job(mock_server)

    applicant = Applicant(
        storage=storage,
        resolver=FakeResolver(),
        headless=True,
        hands=BrowserHands(enabled=False),
        wait_for_human=True,
    )

    block = storage.block_job(job.id, reason="unknown_question", detail="Need salary")
    assert not storage.is_block_resolved(block.id)

    class MockLocator:
        def count(self):
            return 0

    class MockPage:
        def is_closed(self):
            return False
        def wait_for_timeout(self, ms):
            pass
        def locator(self, sel):
            return MockLocator()

    def resume_later():
        import time
        time.sleep(0.1)
        storage.resume_job(job.id)

    t = threading.Thread(target=resume_later)
    t.start()

    resolved = applicant._wait_for_block_resolution(MockPage(), job.id, block, timeout_seconds=2)
    t.join()

    assert resolved is True
    assert storage.is_block_resolved(block.id)
    journey = storage.list_journey_steps(job.id)
    assert any(s.step == "block_resolved" for s in journey)

