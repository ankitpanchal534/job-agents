from __future__ import annotations

from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest

from jobagent.dashboard import filter_jobs, get_metrics
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
