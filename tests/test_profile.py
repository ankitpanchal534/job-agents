import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jobagent.profile import load_profile, profile_warnings

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = Path(__file__).resolve().parent / "fixtures" / "profile"

FILLED = {
    "personal.yaml": {"full_name": "A. Person", "email": "a@example.com", "phone": "1", "city": "X", "country": "Y"},
    "experience.yaml": {
        "roles": [
            {
                "company": "Acme",
                "title": "Frontend Engineer",
                "start": "2022-01",
                "end": "present",
                "summary_short": "Built UIs.",
            }
        ]
    },
    "skills.yaml": {"skills": [{"name": "React", "years": 3, "level": "advanced", "tier": "core"}]},
}


@pytest.fixture
def profile_dir(tmp_path):
    target = tmp_path / "profile"
    shutil.copytree(SHIPPED, target)
    return target


def write(profile_dir, name, data):
    (profile_dir / name).write_text(yaml.safe_dump(data), encoding="utf-8")


def fill(profile_dir):
    for name, data in FILLED.items():
        write(profile_dir, name, data)


def test_real_profile_files_are_valid():
    load_profile(ROOT / "profile")


def test_shipped_templates_load():
    profile = load_profile(SHIPPED)
    assert profile.skills.skills == []
    assert profile.experience.roles == []
    assert profile.answers.faq == []
    assert profile.personal.full_name == ""


def test_empty_templates_produce_warnings_that_name_what_to_fill():
    joined = "\n".join(profile_warnings(load_profile(SHIPPED)))
    assert "personal.full_name" in joined
    assert "personal.email" in joined
    assert "skills" in joined
    assert "experience" in joined


def test_a_filled_profile_has_no_warnings(profile_dir):
    fill(profile_dir)
    assert profile_warnings(load_profile(profile_dir)) == []


def test_unknown_skill_level_is_rejected(profile_dir):
    write(profile_dir, "skills.yaml", {"skills": [{"name": "React", "years": 3, "level": "god", "tier": "core"}]})
    with pytest.raises(ValidationError, match="level"):
        load_profile(profile_dir)


def test_unknown_skill_tier_is_rejected(profile_dir):
    write(profile_dir, "skills.yaml", {"skills": [{"name": "React", "years": 3, "level": "expert", "tier": "main"}]})
    with pytest.raises(ValidationError, match="tier"):
        load_profile(profile_dir)


def test_role_start_must_be_year_month(profile_dir):
    role = dict(FILLED["experience.yaml"]["roles"][0], start="2022")
    write(profile_dir, "experience.yaml", {"roles": [role]})
    with pytest.raises(ValidationError, match="start"):
        load_profile(profile_dir)


def test_role_end_accepts_year_month_or_present_only(profile_dir):
    base = FILLED["experience.yaml"]["roles"][0]
    write(profile_dir, "experience.yaml", {"roles": [dict(base, end="2023-06")]})
    assert load_profile(profile_dir).experience.roles[0].end == "2023-06"

    write(profile_dir, "experience.yaml", {"roles": [dict(base, end="now")]})
    with pytest.raises(ValidationError, match="end"):
        load_profile(profile_dir)


def test_missing_profile_file_is_reported_by_name(profile_dir):
    (profile_dir / "skills.yaml").unlink()
    with pytest.raises(FileNotFoundError, match="skills.yaml"):
        load_profile(profile_dir)


def test_save_learned_answer(profile_dir):
    from jobagent.profile import save_learned_answer
    answers_file = profile_dir / "answers.yaml"

    # Save a new compensation question
    ok = save_learned_answer(answers_file, "Current Compensation (Yearly)", "15,00,000 INR")
    assert ok is True

    # Reload profile to verify it validates strictly
    prof = load_profile(profile_dir)
    faq_questions = [item.question for item in prof.answers.faq]
    assert "Current Compensation (Yearly)" in faq_questions

    # Verify duplicate call succeeds idempotently
    ok2 = save_learned_answer(answers_file, "Current Compensation (Yearly)", "15,00,000 INR")
    assert ok2 is True

