from datetime import datetime, timedelta, timezone

import pytest

from jobagent.storage import STATUSES, DuplicateJobError, Storage

URL = "https://boards.greenhouse.io/acme/jobs/123"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, minutes=1):
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def storage(tmp_path, clock):
    return Storage(tmp_path / "data" / "jobs.db", clock=clock)


def test_add_job_saves_the_link_as_a_new_job(storage):
    job = storage.add_job(URL, source="greenhouse")

    assert job.id > 0
    assert job.url == URL
    assert job.source == "greenhouse"
    assert job.status == "new"
    assert job.score is None
    assert job.created_at == job.updated_at == "2026-11-03T10:00:00+00:00"


def test_duplicate_url_is_rejected_even_with_slash_fragment_and_spaces(storage):
    storage.add_job(URL)

    for variant in (URL + "/", URL + "#apply", "  " + URL + "  ", URL.upper().replace("/ACME/JOBS/123", "/acme/jobs/123")):
        with pytest.raises(DuplicateJobError):
            storage.add_job(variant)

    assert len(storage.list_jobs()) == 1


def test_different_paths_are_different_jobs(storage):
    storage.add_job(URL)
    storage.add_job("https://boards.greenhouse.io/acme/jobs/124")

    assert len(storage.list_jobs()) == 2


def test_get_job_by_url_finds_an_equivalent_link(storage):
    saved = storage.add_job(URL)

    assert storage.get_job_by_url(URL + "/").id == saved.id
    assert storage.get_job_by_url("https://example.com/other") is None


def test_update_job_stores_details_and_bumps_updated_at(storage, clock):
    job = storage.add_job(URL)
    clock.advance(5)

    updated = storage.update_job(job.id, title="Backend Engineer", company="Acme", jd_summary="Python, SQL", score=82)

    assert (updated.title, updated.company, updated.jd_summary, updated.score) == ("Backend Engineer", "Acme", "Python, SQL", 82)
    assert updated.created_at == "2026-11-03T10:00:00+00:00"
    assert updated.updated_at == "2026-11-03T10:05:00+00:00"


def test_update_job_rejects_unknown_fields_and_leaves_the_job_unchanged(storage):
    job = storage.add_job(URL)

    with pytest.raises(ValueError, match="url"):
        storage.update_job(job.id, url="https://evil.example/")
    with pytest.raises(ValueError, match="nonsense"):
        storage.update_job(job.id, nonsense=1)

    assert storage.get_job(job.id).url == URL


def test_status_can_move_through_every_status(storage):
    job = storage.add_job(URL)

    for status in STATUSES:
        assert storage.set_status(job.id, status).status == status


def test_invalid_status_is_rejected(storage):
    job = storage.add_job(URL)

    with pytest.raises(ValueError, match="done"):
        storage.set_status(job.id, "done")
    assert storage.get_job(job.id).status == "new"


def test_list_jobs_filters_by_status_in_insertion_order(storage):
    first = storage.add_job(URL)
    second = storage.add_job(URL + "/2")
    storage.add_job(URL + "/3")
    storage.set_status(first.id, "watch_later")
    storage.set_status(second.id, "watch_later")

    assert [j.id for j in storage.list_jobs(status="watch_later")] == [first.id, second.id]
    assert len(storage.list_jobs()) == 3


def test_queue_answer_with_a_draft_waits_for_review(storage):
    item = storage.queue_answer("Why this company?", answer="Because of the product.")

    assert (item.status, item.source, item.answer) == ("pending_review", "generated", "Because of the product.")


def test_queue_answer_without_a_draft_needs_input(storage):
    item = storage.queue_answer("What is your salary expectation?")

    assert (item.status, item.source, item.answer) == ("needs_input", "human", None)


def test_same_question_is_queued_once_regardless_of_case_and_punctuation(storage):
    first = storage.queue_answer("Why this company?", answer="x")
    second = storage.queue_answer("why THIS company", answer="y")

    assert first.id == second.id
    assert len(storage.list_answers()) == 1


def test_approving_a_draft_keeps_it_generated_and_editing_it_makes_it_human(storage):
    draft = storage.queue_answer("Q1?", answer="draft")
    approved = storage.approve_answer(draft.id)
    assert (approved.status, approved.answer, approved.source) == ("approved", "draft", "generated")

    edited = storage.approve_answer(storage.queue_answer("Q2?", answer="draft2").id, final_answer="mine")
    assert (edited.status, edited.answer, edited.source) == ("approved", "mine", "human")


def test_approving_a_needs_input_item_requires_an_answer(storage):
    item = storage.queue_answer("Salary?")

    with pytest.raises(ValueError, match="answer"):
        storage.approve_answer(item.id)

    approved = storage.approve_answer(item.id, final_answer="12 LPA")
    assert (approved.status, approved.answer, approved.source) == ("approved", "12 LPA", "human")


def test_only_approved_answers_form_the_bank(storage):
    kept = storage.approve_answer(storage.queue_answer("Q1?", answer="a1").id)
    storage.queue_answer("Q2?", answer="a2")
    storage.reject_answer(storage.queue_answer("Q3?", answer="a3").id)

    assert storage.approved_answers() == [(kept.question, "a1")]


def test_a_rejected_question_can_be_queued_again(storage):
    first = storage.queue_answer("Q?", answer="a")
    storage.reject_answer(first.id)

    again = storage.queue_answer("Q?", answer="b")

    assert again.id != first.id
    assert again.status == "pending_review"


def test_list_answers_filters_by_status(storage):
    storage.queue_answer("Q1?", answer="a")
    storage.queue_answer("Q2?")

    assert [a.question for a in storage.list_answers(status="needs_input")] == ["Q2?"]
    assert len(storage.list_answers()) == 2


def test_an_answer_can_be_linked_to_a_job(storage):
    job = storage.add_job(URL)

    assert storage.queue_answer("Q?", answer="a", job_id=job.id).job_id == job.id


def test_jobs_survive_reopening_the_database(tmp_path, clock):
    path = tmp_path / "jobs.db"
    saved = Storage(path, clock=clock).add_job(URL, source="greenhouse")

    reopened = Storage(path, clock=clock)

    assert reopened.get_job(saved.id).url == URL


def add_evaluation(storage, job, **overrides):
    values = dict(
        extracted={"title": "Frontend Engineer", "must_have_skills": ["React"]},
        filter_reasons=[],
        score=86.0,
        missing_must_haves=["TypeScript"],
        decision="apply",
        reason="score 86 is at or above 75",
        model="huggingface/meta-llama/Llama-3.1-8B-Instruct",
    )
    return storage.add_evaluation(job.id, **{**values, **overrides})


def test_an_evaluation_is_saved_with_its_structured_fields(storage):
    job = storage.add_job(URL)

    saved = add_evaluation(storage, job)

    assert saved.job_id == job.id
    assert saved.extracted["must_have_skills"] == ["React"]
    assert saved.missing_must_haves == ["TypeScript"]
    assert (saved.score, saved.decision, saved.filter_reasons) == (86.0, "apply", [])
    assert saved.created_at == "2026-11-03T10:00:00+00:00"


def test_a_job_keeps_every_evaluation_in_order(storage, clock):
    job = storage.add_job(URL)
    first = add_evaluation(storage, job, decision="watch_later", score=60.0)
    clock.advance()
    second = add_evaluation(storage, job)

    assert [e.id for e in storage.list_evaluations(job.id)] == [first.id, second.id]
    assert storage.list_evaluations(job.id + 1) == []


def test_an_evaluation_without_a_score_can_be_saved(storage):
    job = storage.add_job(URL)

    assert add_evaluation(storage, job, score=None, decision="reject", filter_reasons=["blacklisted"]).score is None


def test_an_unknown_decision_is_rejected(storage):
    job = storage.add_job(URL)

    with pytest.raises(ValueError, match="maybe"):
        add_evaluation(storage, job, decision="maybe")


PAGE = "https://boards.greenhouse.io/acme/jobs/123/apply"


def blocked_job(storage, url=URL, reason="captcha", **kwargs):
    job = storage.add_job(url)
    storage.set_status(job.id, "queued")
    return job, storage.block_job(job.id, reason, **kwargs)


def test_blocking_a_job_marks_it_blocked_and_records_why(storage):
    job, block = blocked_job(
        storage, detail="Cloudflare check on the apply page", page_url=PAGE, screenshot_path="shots/1.png"
    )

    assert storage.get_job(job.id).status == "blocked"
    assert (block.job_id, block.reason, block.detail) == (job.id, "captcha", "Cloudflare check on the apply page")
    assert (block.page_url, block.screenshot_path, block.host) == (PAGE, "shots/1.png", "boards.greenhouse.io")
    assert block.resolved_at is None and block.notified_at is None


def test_the_block_host_falls_back_to_the_job_link(storage):
    _, block = blocked_job(storage, url="https://jobs.lever.co/acme/1")

    assert block.host == "jobs.lever.co"


def test_an_unknown_block_reason_is_rejected(storage):
    job = storage.add_job(URL)

    with pytest.raises(ValueError, match="robot"):
        storage.block_job(job.id, "robot")


def test_blocking_a_job_that_does_not_exist_is_rejected(storage):
    with pytest.raises(ValueError, match="no job"):
        storage.block_job(99, "captcha")


def test_the_same_open_block_is_not_recorded_twice(storage):
    job, first = blocked_job(storage)

    second = storage.block_job(job.id, "captcha")

    assert second.id == first.id
    assert len(storage.list_blocks()) == 1


def test_resuming_puts_the_job_back_in_the_apply_queue_and_resolves_its_blocks(storage, clock):
    job, _ = blocked_job(storage)
    clock.advance(5)

    resumed = storage.resume_job(job.id)

    assert resumed.status == "queued"
    assert storage.list_blocks() == []
    assert storage.list_blocks(open_only=False)[0].resolved_at == "2026-11-03T10:05:00+00:00"


def test_only_a_blocked_job_can_be_resumed(storage):
    job = storage.add_job(URL)

    with pytest.raises(ValueError, match="new"):
        storage.resume_job(job.id)
    with pytest.raises(ValueError, match="no job"):
        storage.resume_job(99)


def test_open_blocks_are_listed_oldest_first(storage):
    _, first = blocked_job(storage, url=URL + "/1")
    _, second = blocked_job(storage, url=URL + "/2", reason="login_expired")

    assert [b.id for b in storage.list_blocks()] == [first.id, second.id]


def test_alert_bookkeeping_tracks_the_last_alert_and_the_ones_held_back(storage, clock):
    _, first = blocked_job(storage, url=URL + "/1")
    clock.advance()
    _, second = blocked_job(storage, url=URL + "/2")
    clock.advance()
    _, third = blocked_job(storage, url=URL + "/3")
    key = ("captcha", first.host)
    assert storage.last_alert(*key) is None

    storage.mark_block_notified(first.id)

    assert storage.last_alert(*key) == "2026-11-03T10:02:00+00:00"
    assert storage.unalerted_count(*key, since=storage.last_alert(*key), exclude_id=third.id) == 0
    assert storage.unalerted_count(*key, since=None, exclude_id=third.id) == 1
