import json

import pytest
from fakes import FakeLLM
from matching import EXTRACTION, JD_TEXT, URL, make_llm, make_matcher, scoring

from jobagent.jd import MatchError
from jobagent.matcher import match_job
from jobagent.storage import Storage


def test_a_strong_match_scores_by_weighted_coverage_and_is_applied_for():
    llm = make_llm()

    result = make_matcher(llm).evaluate(JD_TEXT)

    assert result.score == 85.7
    assert result.decision == "apply"
    assert result.missing_must_haves == []
    assert result.summary == "A frontend role at Acme."
    assert "85.7" in result.reason and "Fair fit." in result.reason
    assert llm.tasks == ["jd_extraction", "skill_scoring", "jd_summary"]


def test_a_partial_match_is_kept_as_watch_later():
    result = make_matcher(make_llm(skill_scoring=scoring(typescript="partial"))).evaluate(JD_TEXT)

    assert (result.score, result.decision) == (64.3, "watch_later")


def test_a_weak_match_is_rejected_and_names_the_missing_must_haves():
    result = make_matcher(make_llm(skill_scoring=scoring(react="none", typescript="none"))).evaluate(JD_TEXT)

    assert (result.score, result.decision) == (0.0, "reject")
    assert result.missing_must_haves == ["React", "TypeScript"]
    assert "React, TypeScript" in result.reason


def test_the_decision_thresholds_come_from_settings():
    def lower_the_bar(prefs, settings):
        settings.matcher.apply_threshold = 60

    result = make_matcher(make_llm(skill_scoring=scoring(typescript="partial")), lower_the_bar).evaluate(JD_TEXT)

    assert (result.score, result.decision) == (64.3, "apply")


def test_the_score_uses_the_configured_weights():
    def equal_weights(prefs, settings):
        settings.matcher.must_have_weight = 1
        settings.matcher.nice_to_have_weight = 1

    result = make_matcher(make_llm(), equal_weights).evaluate(JD_TEXT)

    assert (result.score, result.decision) == (66.7, "watch_later")


def test_a_failed_hard_filter_rejects_without_scoring_or_summarising():
    def blacklist_acme(prefs, settings):
        prefs.blacklist.companies = ["Acme"]

    llm = make_llm()

    result = make_matcher(llm, blacklist_acme).evaluate(JD_TEXT)

    assert (result.decision, result.score) == ("reject", None)
    assert "blacklisted" in result.reason
    assert result.filter_reasons and result.summary == ""
    assert llm.tasks == ["jd_extraction"]


def test_a_posting_that_lists_no_skills_is_kept_for_the_owner_to_look_at():
    llm = make_llm(extraction=json.dumps({"title": "Frontend Engineer", "company": "Acme"}))

    result = make_matcher(llm).evaluate(JD_TEXT)

    assert (result.decision, result.score) == ("watch_later", None)
    assert "no skills" in result.reason
    assert llm.tasks == ["jd_extraction", "jd_summary"]


def test_a_skill_missing_from_the_models_reply_counts_as_not_matched_and_names_are_case_insensitive():
    partial_reply = json.dumps({"must_have": [{"skill": "react", "match": "full"}], "nice_to_have": []})

    result = make_matcher(make_llm(skill_scoring=partial_reply)).evaluate(JD_TEXT)

    assert result.missing_must_haves == ["TypeScript"]
    assert result.score == 42.9


def test_an_unreadable_scoring_reply_is_reported():
    with pytest.raises(MatchError, match="scoring"):
        make_matcher(make_llm(skill_scoring="no idea")).evaluate(JD_TEXT)


def test_the_scoring_prompt_has_the_skills_but_neither_the_job_text_nor_contact_details():
    llm = make_llm()

    make_matcher(llm).evaluate(JD_TEXT)

    _, system, user = next(call for call in llm.calls if call[0] == "skill_scoring")
    prompt = system + user
    assert "React" in prompt and "advanced" in prompt and "GraphQL" in prompt
    assert JD_TEXT not in prompt
    assert "asha@example.com" not in prompt and "99999" not in prompt


def test_the_summary_prompt_sends_the_job_text_as_delimited_untrusted_data():
    llm = make_llm()

    make_matcher(llm).evaluate(JD_TEXT)

    _, system, user = next(call for call in llm.calls if call[0] == "jd_summary")
    assert "untrusted" in system.lower()
    assert f"<job_description>\n{JD_TEXT}\n</job_description>" in user


def test_model_label_names_the_models_used():
    label = make_matcher(make_llm()).model_label

    assert "jd_extraction=local/qwen2.5:7b" in label and "skill_scoring=local/qwen2.5:7b" in label


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "jobs.db")


def test_an_applicable_job_is_saved_to_the_tracker_as_queued_with_its_evaluation(storage):
    outcome = match_job(storage, make_matcher(make_llm()), URL, JD_TEXT, source="greenhouse")

    job = outcome.job
    assert (job.status, job.score, job.title, job.company) == ("queued", 85.7, "Frontend Engineer", "Acme")
    assert job.jd_summary == "A frontend role at Acme." and job.source == "greenhouse"
    evaluation = storage.list_evaluations(job.id)[0]
    assert evaluation.decision == "apply" and evaluation.extracted["title"] == "Frontend Engineer"
    assert "skill_scoring=" in evaluation.model


@pytest.mark.parametrize(
    "reply, status",
    [(scoring(typescript="partial"), "watch_later"), (scoring(react="none", typescript="none"), "rejected")],
)
def test_other_decisions_map_to_watch_later_and_rejected(storage, reply, status):
    outcome = match_job(storage, make_matcher(make_llm(skill_scoring=reply)), URL, JD_TEXT)

    assert outcome.job.status == status


def test_a_job_already_evaluated_is_not_evaluated_again_unless_forced(storage):
    llm = make_llm()
    matcher = make_matcher(llm)
    first = match_job(storage, matcher, URL, JD_TEXT)
    calls_after_first = len(llm.calls)

    again = match_job(storage, matcher, URL + "/", JD_TEXT)

    assert again.skipped and again.job.id == first.job.id
    assert len(llm.calls) == calls_after_first
    assert len(storage.list_evaluations(first.job.id)) == 1

    forced = match_job(storage, matcher, URL, JD_TEXT, force=True)
    assert not forced.skipped
    assert len(storage.list_evaluations(first.job.id)) == 2


def test_a_job_that_is_already_applied_for_is_never_evaluated_again_even_when_forced(storage):
    llm = make_llm()
    job = storage.add_job(URL)
    storage.set_status(job.id, "applied")

    outcome = match_job(storage, make_matcher(llm), URL, JD_TEXT, force=True)

    assert outcome.skipped and outcome.job.status == "applied"
    assert llm.calls == []


def test_a_failed_evaluation_leaves_the_link_tracked_as_new(storage):
    with pytest.raises(MatchError):
        match_job(storage, make_matcher(make_llm(extraction="nonsense")), URL, JD_TEXT)

    job = storage.get_job_by_url(URL)
    assert job.status == "new"
    assert storage.list_evaluations(job.id) == []
