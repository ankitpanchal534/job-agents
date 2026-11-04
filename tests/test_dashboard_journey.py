from __future__ import annotations

from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest

from jobagent.storage import Storage

DASHBOARD_APP = Path(__file__).resolve().parents[1] / "src" / "jobagent" / "dashboard.py"


def test_dashboard_renders_journey_steps(tmp_path, monkeypatch):
    db = tmp_path / "test_journey_render.db"
    monkeypatch.setenv("JOBAGENT_DB", str(db))
    storage = Storage(str(db))

    job = storage.add_job("https://example.com/software-engineer")
    storage.update_job(job.id, company="TechCorp", title="Lead Python Architect", score=92.0)

    # Add journey steps
    storage.add_journey_step(
        job_id=job.id,
        step="extractor",
        agent="ExtractorAgent",
        status="success",
        summary="Extracted role 'Lead Python Architect' at 'TechCorp'",
        details={"company": "TechCorp", "title": "Lead Python Architect"},
    )
    storage.add_journey_step(
        job_id=job.id,
        step="matcher",
        agent="MatcherAgent",
        status="success",
        summary="Decision: APPLY (Score: 92%) - Strong skill match",
        details={"score": 92.0, "decision": "apply"},
    )
    storage.add_journey_step(
        job_id=job.id,
        step="field_input",
        agent="BrowserApplicant",
        status="success",
        summary="Filled field 'Full Name'",
        details={"field": "Full Name", "value": "Alice Doe"},
    )

    app = AppTest.from_file(str(DASHBOARD_APP), default_timeout=30).run()
    assert not app.exception
