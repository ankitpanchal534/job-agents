from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Optional, Union

from pydantic import Field, StringConstraints

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
