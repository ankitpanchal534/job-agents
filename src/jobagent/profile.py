from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from pydantic import Field, StringConstraints

import yaml
from jobagent.config import ROOT, StrictModel, read_yaml

DEFAULT_PROFILE_DIR = ROOT / "profile"

YearMonth = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]


class Links(StrictModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    other: list[str] = []


class Personal(StrictModel):
    full_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    languages: list[str] = []
    links: Links = Links()


class Role(StrictModel):
    company: str
    title: str
    start: YearMonth
    end: Union[YearMonth, Literal["present"]]
    location: str = ""
    summary_short: str = ""
    summary_medium: str = ""
    summary_long: str = ""
    achievements: list[str] = []
    tech: list[str] = []


class Experience(StrictModel):
    roles: list[Role] = []


class Degree(StrictModel):
    institution: str
    degree: str
    field: str = ""
    start_year: int
    end_year: Optional[int] = None
    grade: str = ""


class Education(StrictModel):
    degrees: list[Degree] = []


class Skill(StrictModel):
    name: str
    years: float = Field(ge=0)
    level: Literal["beginner", "intermediate", "advanced", "expert"]
    tier: Literal["core", "secondary"]


class Skills(StrictModel):
    skills: list[Skill] = []


class FaqEntry(StrictModel):
    question: str
    answer: str
    tags: list[str] = []


class Answers(StrictModel):
    faq: list[FaqEntry] = []


class Profile(StrictModel):
    personal: Personal
    experience: Experience
    education: Education
    skills: Skills
    answers: Answers


def load_profile(directory: Path = DEFAULT_PROFILE_DIR) -> Profile:
    directory = Path(directory)
    return Profile(
        personal=Personal.model_validate(read_yaml(directory / "personal.yaml")),
        experience=Experience.model_validate(read_yaml(directory / "experience.yaml")),
        education=Education.model_validate(read_yaml(directory / "education.yaml")),
        skills=Skills.model_validate(read_yaml(directory / "skills.yaml")),
        answers=Answers.model_validate(read_yaml(directory / "answers.yaml")),
    )


def profile_warnings(profile: Profile) -> list[str]:
    warnings: list[str] = []
    if not profile.personal.full_name:
        warnings.append("personal.full_name is empty: forms cannot be filled without it.")
    if not profile.personal.email:
        warnings.append("personal.email is empty: forms and email verification need it.")
    if not profile.skills.skills:
        warnings.append("skills.yaml has no skills: the matcher cannot score any job.")
    if not profile.experience.roles:
        warnings.append("experience.yaml has no roles: answers cannot be drafted from your history.")
    return warnings


def save_learned_answer(
    answers_file: Path,
    question: str,
    answer: str,
    tags: Optional[list[str]] = None,
) -> bool:
    """Appends or updates a learned FAQ answer in profile/answers.yaml cleanly."""
    answers_file = Path(answers_file)
    if not answers_file.exists():
        return False

    q_clean = question.strip()
    a_clean = answer.strip()
    if not q_clean or not a_clean:
        return False

    content = answers_file.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(content) or {}
    except Exception:
        data = {}

    faq_list = data.get("faq", [])
    from jobagent.text import normalize_question

    norm_q = normalize_question(q_clean)
    for item in faq_list:
        if isinstance(item, dict) and normalize_question(str(item.get("question", ""))) == norm_q:
            return True

    tag_list = list(tags) if tags else ["learned"]
    q_lower = q_clean.lower()
    if any(k in q_lower for k in ("compensation", "salary", "ctc", "pay")):
        if "compensation" not in tag_list:
            tag_list.append("compensation")
    elif any(k in q_lower for k in ("notice", "joining", "available")):
        if "notice" not in tag_list:
            tag_list.append("notice")
    elif any(k in q_lower for k in ("relocate", "relocation", "location", "city")):
        if "location" not in tag_list:
            tag_list.append("location")

    q_escaped = q_clean.replace('"', '\\"')
    a_escaped = a_clean.replace('"', '\\"')
    tag_str = ", ".join(tag_list)
    entry = f'\n  - question: "{q_escaped}"\n    answer: "{a_escaped}"\n    tags: [{tag_str}]\n'

    # If file has `faq: []`, replace with `faq:` so items can follow
    import re
    cleaned_content = re.sub(r"faq:\s*\[\s*\]", "faq:", content)

    if "faq:" in cleaned_content:
        new_content = cleaned_content.rstrip() + entry
    else:
        new_content = cleaned_content.rstrip() + f"\nfaq:{entry}"

    try:
        parsed = yaml.safe_load(new_content)
        Answers.model_validate(parsed)
    except Exception:
        return False

    answers_file.write_text(new_content, encoding="utf-8")
    return True

