import json
from pathlib import Path

from fakes import FakeLLM

from jobagent.config import load_preferences, load_settings
from jobagent.matcher import Matcher
from jobagent.profile import Answers, Education, Experience, Personal, Profile, Skill, Skills

FIXTURES = Path(__file__).resolve().parent / "fixtures"
URL = "https://boards.greenhouse.io/acme/jobs/123"
JD_TEXT = "Acme is hiring a Frontend Engineer. React and TypeScript required; GraphQL a plus."

EXTRACTION = json.dumps(
    {
        "title": "Frontend Engineer",
        "company": "Acme",
        "must_have_skills": ["React", "TypeScript"],
        "nice_to_have_skills": ["GraphQL"],
    }
)


def scoring(react="full", typescript="full", graphql="none", reason="Fair fit."):
    return json.dumps(
        {
            "must_have": [{"skill": "React", "match": react}, {"skill": "TypeScript", "match": typescript}],
            "nice_to_have": [{"skill": "GraphQL", "match": graphql}],
            "reason": reason,
        }
    )


def make_llm(extraction=EXTRACTION, skill_scoring=None, summary="A frontend role at Acme."):
    return FakeLLM(jd_extraction=extraction, skill_scoring=skill_scoring or scoring(), jd_summary=summary)


def make_matcher(llm, configure=None):
    prefs = load_preferences(FIXTURES / "preferences.yaml")
    settings = load_settings(FIXTURES / "settings.yaml")
    if configure:
        configure(prefs, settings)
    profile = Profile(
        personal=Personal(full_name="Asha Rao", email="asha@example.com", phone="+91 99999 00000"),
        experience=Experience(),
        education=Education(),
        skills=Skills(
            skills=[
                Skill(name="React", years=3, level="advanced", tier="core"),
                Skill(name="TypeScript", years=2, level="intermediate", tier="secondary"),
            ]
        ),
        answers=Answers(),
    )
    return Matcher(profile, prefs, settings, llm)
