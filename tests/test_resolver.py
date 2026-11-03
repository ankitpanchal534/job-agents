from pathlib import Path

import pytest
from fakes import FakeLLM

from jobagent.config import load_preferences, load_settings
from jobagent.profile import (
    Answers,
    Education,
    Experience,
    FaqEntry,
    Links,
    Personal,
    Profile,
    Role,
    Skill,
    Skills,
)
from jobagent.resolver import Resolver

FIXTURES = Path(__file__).resolve().parent / "fixtures"

EMAIL = "asha@example.com"
PHONE = "+91 99999 00000"
LINKEDIN = "https://linkedin.com/in/asha-rao"


def make_profile(faq=()):
    return Profile(
        personal=Personal(
            full_name="Asha Rao", email=EMAIL, phone=PHONE, city="Pune", country="India", links=Links(linkedin=LINKEDIN)
        ),
        experience=Experience(
            roles=[
                Role(
                    company="Acme",
                    title="Frontend Engineer",
                    start="2022-01",
                    end="present",
                    summary_short="Built React UIs.",
                    summary_medium="Built React dashboards used by 5,000 people.",
                )
            ]
        ),
        education=Education(),
        skills=Skills(skills=[Skill(name="React", years=3, level="advanced", tier="core")]),
        answers=Answers(faq=[FaqEntry(question=q, answer=a) for q, a in faq]),
    )


def make_resolver(faq=(), llm=None, extra_bank=(), relocate=False):
    prefs = load_preferences(FIXTURES / "preferences.yaml")
    prefs.locations.open_to_relocation = relocate
    llm = llm or FakeLLM()
    resolver = Resolver(make_profile(faq), prefs, load_settings(FIXTURES / "settings.yaml"), llm, extra_bank=extra_bank)
    return resolver, llm


@pytest.mark.parametrize(
    "question, expected",
    [
        ("Email address", EMAIL),
        ("What is your phone number?", PHONE),
        ("Full name", "Asha Rao"),
        ("First name", "Asha"),
        ("Last name", "Rao"),
        ("LinkedIn profile URL", LINKEDIN),
        ("Current company", "Acme"),
        ("Current job title", "Frontend Engineer"),
        ("City", "Pune"),
        ("Country", "India"),
    ],
)
def test_plain_fields_come_straight_from_the_profile_without_the_llm(question, expected):
    resolver, llm = make_resolver()

    result = resolver.resolve(question)

    assert (result.answer, result.source, result.needs_review) == (expected, "field", False)
    assert llm.calls == []


def test_a_curated_answer_matches_ignoring_case_and_punctuation_and_is_used_verbatim():
    resolver, llm = make_resolver(faq=[("Why do you want to work here?", "I like your product.")])

    result = resolver.resolve("why DO you want to work here")

    assert (result.answer, result.source, result.needs_review) == ("I like your product.", "bank", False)
    assert llm.calls == []


def test_a_curated_answer_overrides_the_profile_field():
    resolver, _ = make_resolver(faq=[("Email address", "jobs@example.com")])

    assert resolver.resolve("Email address").answer == "jobs@example.com"


def test_a_near_identical_question_matches_a_curated_answer_without_the_llm():
    resolver, llm = make_resolver(faq=[("Describe your experience with React", "Three years.")])

    result = resolver.resolve("Describe your experience with React JS")

    assert (result.answer, result.source) == ("Three years.", "bank")
    assert llm.calls == []


def test_answers_approved_in_the_review_page_join_the_bank():
    resolver, _ = make_resolver(extra_bank=[("Do you have a driving licence?", "Yes")])

    assert resolver.resolve("Do you have a driving licence?").answer == "Yes"


def test_the_llm_can_match_a_reworded_question_when_it_is_confident():
    llm = FakeLLM(answer_matching='{"index": 0, "confidence": 0.95}')
    resolver, _ = make_resolver(faq=[("Tell us about yourself", "I build UIs.")], llm=llm)

    result = resolver.resolve("Can you introduce yourself briefly?")

    assert (result.answer, result.source) == ("I build UIs.", "bank")
    assert llm.tasks == ["answer_matching"]
    assert "Tell us about yourself" in llm.calls[0][2]


@pytest.mark.parametrize("reply", ['{"index": 0, "confidence": 0.5}', '{"index": null, "confidence": 0.99}', "not json"])
def test_a_weak_or_unusable_llm_match_is_ignored_and_the_question_is_drafted_instead(reply):
    llm = FakeLLM(answer_matching=reply, answer_generation="A fresh draft.")
    resolver, _ = make_resolver(faq=[("Tell us about yourself", "I build UIs.")], llm=llm)

    result = resolver.resolve("Can you introduce yourself briefly?")

    assert (result.answer, result.source, result.needs_review) == ("A fresh draft.", "generated", True)
    assert llm.tasks == ["answer_matching", "answer_generation"]


def test_a_similar_but_different_sensitive_question_never_reuses_a_curated_answer():
    llm = FakeLLM()
    resolver, _ = make_resolver(faq=[("Will you require visa sponsorship in the US?", "Yes")], llm=llm)

    result = resolver.resolve("Will you require visa sponsorship in the UK?")

    assert result.answer is None
    assert "work_authorization" in result.blocked_reason
    assert llm.calls == []


def test_an_identical_sensitive_question_uses_the_curated_answer():
    resolver, _ = make_resolver(faq=[("Will you require visa sponsorship in the US?", "Yes")])

    result = resolver.resolve("Will you require visa sponsorship in the US?")

    assert (result.answer, result.source) == ("Yes", "bank")


@pytest.mark.parametrize("question", ["What is your notice period?", "When can you start?"])
def test_notice_period_comes_from_preferences(question):
    resolver, llm = make_resolver()

    result = resolver.resolve(question)

    assert (result.answer, result.source, result.needs_review) == ("30 days", "preference", False)
    assert llm.calls == []


@pytest.mark.parametrize("relocate, expected", [(True, "Yes"), (False, "No")])
def test_relocation_comes_from_preferences(relocate, expected):
    resolver, _ = make_resolver(relocate=relocate)

    assert resolver.resolve("Are you open to relocation?").answer == expected


@pytest.mark.parametrize(
    "question, category",
    [
        ("What are your salary expectations?", "compensation"),
        ("Do you have a criminal record?", "background"),
        ("Please state your gender", "demographic"),
        ("Are you a veteran?", "demographic"),
        ("Do you have a disability?", "disability"),
        ("Provide two professional references", "references"),
        ("Are you legally authorized to work in the United States?", "work_authorization"),
    ],
)
def test_sensitive_questions_are_blocked_and_never_drafted(question, category):
    llm = FakeLLM(answer_generation="Made up.")
    resolver, _ = make_resolver(llm=llm)

    result = resolver.resolve(question)

    assert result.answer is None
    assert category in result.blocked_reason
    assert llm.calls == []


def test_an_open_question_is_drafted_from_the_profile_and_flagged_for_review():
    llm = FakeLLM(answer_generation="I enjoy building dashboards.")
    resolver, _ = make_resolver(llm=llm)

    result = resolver.resolve("Why do you want to work here?", jd_summary="Acme builds fintech tools.")

    assert (result.answer, result.source, result.needs_review) == ("I enjoy building dashboards.", "generated", True)
    task, system, user = llm.calls[0]
    prompt = system + user
    assert task == "answer_generation"
    assert "Why do you want to work here?" in prompt
    assert "Built React dashboards used by 5,000 people." in prompt
    assert "Acme builds fintech tools." in prompt


def test_the_draft_prompt_leaves_out_contact_details():
    llm = FakeLLM(answer_generation="ok")
    resolver, _ = make_resolver(llm=llm)

    resolver.resolve("Why do you want to work here?")

    prompt = llm.calls[0][1] + llm.calls[0][2]
    for private in (EMAIL, PHONE, LINKEDIN):
        assert private not in prompt


@pytest.mark.parametrize("reply", ["INSUFFICIENT", "  insufficient.  ", ""])
def test_a_draft_the_model_cannot_ground_is_blocked(reply):
    resolver, _ = make_resolver(llm=FakeLLM(answer_generation=reply))

    result = resolver.resolve("Why do you want to work here?")

    assert result.answer is None
    assert "enough" in result.blocked_reason


def test_a_draft_longer_than_the_field_limit_is_blocked():
    resolver, _ = make_resolver(llm=FakeLLM(answer_generation="x" * 50))

    result = resolver.resolve("Why do you want to work here?", max_length=20)

    assert result.answer is None
    assert "20" in result.blocked_reason
