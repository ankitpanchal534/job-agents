import pytest
from jobagent.storage import Storage

def test_add_and_list_journey_steps(tmp_path):
    storage = Storage(tmp_path / "test.db")
    job = storage.add_job("https://jobs.example.com/p/123")

    step1 = storage.add_journey_step(
        job_id=job.id,
        step="extractor",
        agent="ExtractorAgent",
        status="success",
        summary="Extracted JD for Senior Python Engineer at TechCorp",
        details={"company": "TechCorp", "title": "Senior Python Engineer", "skills": ["Python", "Docker"]}
    )

    assert step1.id is not None
    assert step1.job_id == job.id
    assert step1.step == "extractor"
    assert step1.agent == "ExtractorAgent"
    assert step1.status == "success"
    assert step1.summary == "Extracted JD for Senior Python Engineer at TechCorp"
    assert step1.details["company"] == "TechCorp"
    assert step1.details["skills"] == ["Python", "Docker"]
    assert step1.created_at is not None

    step2 = storage.add_journey_step(
        job_id=job.id,
        step="matcher",
        agent="MatcherAgent",
        status="success",
        summary="Match score: 85% (matched: Python, Docker)",
        details={"score": 85.0, "matched": ["Python", "Docker"], "missing": []}
    )

    steps = storage.list_journey_steps(job.id)
    assert len(steps) == 2
    assert steps[0].step == "extractor"
    assert steps[1].step == "matcher"
    assert steps[1].details["score"] == 85.0

def test_journey_step_empty_details(tmp_path):
    storage = Storage(tmp_path / "test.db")
    job = storage.add_job("https://jobs.example.com/p/456")

    step = storage.add_journey_step(
        job_id=job.id,
        step="navigation",
        agent="BrowserApplicant",
        status="info",
        summary="Opened URL and clicked Apply"
    )
    assert step.details is None
    steps = storage.list_journey_steps(job.id)
    assert len(steps) == 1
    assert steps[0].details is None

def test_is_block_resolved(tmp_path):
    storage = Storage(tmp_path / "test.db")
    job = storage.add_job("https://jobs.example.com/p/789")

    block = storage.block_job(job.id, reason="captcha", detail="Cloudflare challenge")
    assert not storage.is_block_resolved(block.id)

    storage.resume_job(job.id)
    assert storage.is_block_resolved(block.id)
