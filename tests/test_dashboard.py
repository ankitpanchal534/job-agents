from __future__ import annotations

from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest

from jobagent.dashboard import filter_jobs, get_metrics
from jobagent.jd import JobPosting
from jobagent.matcher import MatchOutcome, MatchResult
from jobagent.applicant import ApplicantOutcome
from jobagent.storage import Job, Storage

DASHBOARD_APP = Path(__file__).resolve().parents[1] / "src" / "jobagent" / "dashboard.py"


def test_get_metrics(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    j1 = storage.add_job("https://example.com/1")
    j2 = storage.add_job("https://example.com/2")
    storage.set_status(j1.id, "applied")
    storage.set_status(j2.id, "blocked")

    metrics = get_metrics(storage)
    assert metrics["total"] == 2
    assert metrics["applied"] == 1
    assert metrics["blocked"] == 1
    assert metrics["queued"] == 0


def test_filter_jobs():
    jobs = [
        Job(id=1, url="https://example.com/a", source=None, company="Acme Corp", title="Backend Engineer", jd_summary="", status="applied", score=85.0, created_at="2026-11-04T00:00:00", updated_at="2026-11-04T00:00:00"),
        Job(id=2, url="https://example.com/b", source=None, company="Beta Labs", title="Data Scientist", jd_summary="", status="queued", score=70.0, created_at="2026-11-04T00:00:00", updated_at="2026-11-04T00:00:00"),
        Job(id=3, url="https://example.com/c", source=None, company="Gamma AI", title="Fullstack Engineer", jd_summary="", status="blocked", score=90.0, created_at="2026-11-04T00:00:00", updated_at="2026-11-04T00:00:00"),
    ]

    # Filter by status
    assert len(filter_jobs(jobs, status="applied")) == 1
    assert filter_jobs(jobs, status="applied")[0].company == "Acme Corp"

    # Filter by search term
    assert len(filter_jobs(jobs, search="engineer")) == 2
    assert len(filter_jobs(jobs, search="Beta")) == 1


def test_dashboard_renders_cleanly(tmp_path, monkeypatch):
    db = tmp_path / "test_render.db"
    monkeypatch.setenv("JOBAGENT_DB", str(db))
    storage = Storage(str(db))
    storage.add_job("https://example.com/demo")

    app = AppTest.from_file(str(DASHBOARD_APP), default_timeout=30).run()
    assert not app.exception
    assert len(app.tabs) == 4


def test_dashboard_agent_button_execution(tmp_path, monkeypatch):
    db = tmp_path / "test_button.db"
    monkeypatch.setenv("JOBAGENT_DB", str(db))
    storage = Storage(str(db))

    posting = JobPosting(
        title="Frontend Engineer",
        company="TestCorp",
        location="Remote",
        must_have_skills=["React", "TypeScript"],
        nice_to_have_skills=["GraphQL"],
    )
    result = MatchResult(
        posting=posting,
        decision="apply",
        reason="Strong match on React & TypeScript",
        score=92.0,
        filter_reasons=[],
        missing_must_haves=[],
        summary="Frontend role at TestCorp.",
    )
    job = storage.add_job("https://boards.greenhouse.io/demo/jobs/12345")
    outcome = MatchOutcome(job=job, result=result, skipped=False)

    monkeypatch.setattr("jobagent.dashboard.fetch_jd_text", lambda url: "Sample JD text")
    monkeypatch.setattr("jobagent.dashboard.match_job", lambda **kw: outcome)

    def mock_apply(self, job_id, dry_run=True, wait_for_human=True):
        self._on_resolve("Years of experience", "4", "profile/answers.yaml")
        self._on_action("Typing", "Years of experience", "4")
        return ApplicantOutcome(job_id=job_id, status="applied", dry_run=dry_run, detail="Dry run successful")

    monkeypatch.setattr("jobagent.dashboard.Applicant.apply", mock_apply)

    app = AppTest.from_file(str(DASHBOARD_APP), default_timeout=30).run()
    assert not app.exception

    buttons = [b for b in app.button if "Start Application Agent" in b.label]
    assert len(buttons) == 1
    buttons[0].click().run()
    assert not app.exception


def test_dashboard_agent_button_waiting_and_answered_callbacks(tmp_path, monkeypatch):
    db = tmp_path / "test_callbacks.db"
    monkeypatch.setenv("JOBAGENT_DB", str(db))
    storage = Storage(str(db))

    monkeypatch.setattr("jobagent.dashboard.fetch_jd_text", lambda url: "Sample JD text")
    job = storage.add_job("https://boards.greenhouse.io/demo/jobs/12345")
    posting = JobPosting(title="Eng", company="Co", location="Remote", must_have_skills=[], nice_to_have_skills=[])
    result = MatchResult(posting=posting, decision="apply", reason="Match", score=90.0, filter_reasons=[], missing_must_haves=[], summary="")
    monkeypatch.setattr("jobagent.dashboard.match_job", lambda **kw: MatchOutcome(job=job, result=result, skipped=False))

    def mock_apply(self, job_id, dry_run=True, wait_for_human=True):
        self._on_action("Waiting for Human", "Current Compensation (Yearly)", "Chrome open & highlighted")
        self._on_action("Answered", "Current Compensation (Yearly)", "Captured: 25 LPA")
        return ApplicantOutcome(job_id=job_id, status="applied", dry_run=dry_run, detail="Application submitted")

    monkeypatch.setattr("jobagent.dashboard.Applicant.apply", mock_apply)

    app = AppTest.from_file(str(DASHBOARD_APP), default_timeout=30).run()
    assert not app.exception

    buttons = [b for b in app.button if "Start Application Agent" in b.label]
    assert len(buttons) == 1
    buttons[0].click().run()
    assert not app.exception
