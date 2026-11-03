from pathlib import Path

import pytest

from jobagent.config import WorkAuthorization, load_preferences
from jobagent.filters import hard_filter_reasons
from jobagent.jd import JobPosting

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def posting(**fields):
    return JobPosting(**{"title": "Frontend Engineer", "company": "Acme", **fields})


def prefs():
    return load_preferences(FIXTURES / "preferences.yaml")


def reasons(job, text="A normal job description.", **changes):
    p = prefs()
    for dotted, value in changes.items():
        section, _, name = dotted.partition("__")
        setattr(getattr(p, section), name, value)
    return hard_filter_reasons(job, text, p)


def test_an_unremarkable_posting_passes_the_default_preferences():
    assert reasons(posting()) == []


def test_a_blacklisted_company_is_rejected_case_insensitively_and_by_part_of_the_name():
    p = prefs()
    p.blacklist.companies = ["Deloitte"]

    assert any("blacklisted" in r for r in hard_filter_reasons(posting(company="DELOITTE Consulting"), "", p))
    assert hard_filter_reasons(posting(company="Acme"), "", p) == []


def test_a_blacklisted_phrase_in_the_description_is_rejected():
    p = prefs()
    p.blacklist.description_keywords = ["security clearance"]

    found = hard_filter_reasons(posting(), "Must hold an active Security Clearance.", p)

    assert any("security clearance" in r for r in found)


def test_the_title_must_match_one_of_the_target_roles_when_roles_are_set():
    p = prefs()
    p.target.roles = ["Frontend Engineer", "React.js Developer"]

    assert hard_filter_reasons(posting(title="Senior Frontend Engineer (React)"), "", p) == []
    assert hard_filter_reasons(posting(title="react js developer"), "", p) == []
    assert any("target roles" in r for r in hard_filter_reasons(posting(title="Backend Engineer"), "", p))


def test_excluded_title_words_match_whole_words_only():
    p = prefs()
    p.target.exclude_title_keywords = ["intern"]

    assert any("intern" in r for r in hard_filter_reasons(posting(title="Frontend Intern"), "", p))
    assert hard_filter_reasons(posting(title="International Sales Engineer"), "", p) == []


def test_seniority_outside_the_list_is_rejected_but_unknown_seniority_passes():
    p = prefs()
    p.target.seniority = ["junior", "mid"]

    assert any("seniority" in r for r in hard_filter_reasons(posting(seniority="senior"), "", p))
    assert hard_filter_reasons(posting(seniority="mid"), "", p) == []
    assert hard_filter_reasons(posting(seniority=None), "", p) == []


def test_years_required_above_the_limit_is_rejected():
    p = prefs()
    p.target.years_experience.max = 3

    assert any("years" in r for r in hard_filter_reasons(posting(years_required_min=5), "", p))
    assert hard_filter_reasons(posting(years_required_min=2), "", p) == []
    assert hard_filter_reasons(posting(), "", p) == []


def test_a_job_type_outside_the_list_is_rejected():
    assert any("job type" in r for r in reasons(posting(job_type="contract")))
    assert reasons(posting(job_type="full-time")) == []


def test_remote_only_rejects_onsite_and_hybrid_jobs():
    for mode in ("onsite", "hybrid"):
        assert any("remote" in r for r in reasons(posting(remote=mode), locations__remote="only"))
    assert reasons(posting(remote="remote"), locations__remote="only") == []


def test_onsite_only_rejects_remote_jobs():
    assert any("on-site" in r for r in reasons(posting(remote="remote"), locations__remote="no"))


def test_location_must_match_a_country_or_city_unless_the_job_is_remote():
    p = prefs()
    p.locations.countries = ["India"]
    p.locations.cities = ["Bengaluru"]

    assert any("location" in r for r in hard_filter_reasons(posting(remote="onsite", country="Germany", city="Berlin"), "", p))
    assert hard_filter_reasons(posting(remote="onsite", country="India", city="Pune"), "", p) == []
    assert hard_filter_reasons(posting(remote="onsite", country="Germany", city="Bengaluru"), "", p) == []
    assert hard_filter_reasons(posting(remote="remote", country="Germany"), "", p) == []
    assert hard_filter_reasons(posting(remote="onsite"), "", p) == []


def test_pay_below_the_floor_is_rejected_after_matching_currency_and_period():
    p = prefs()
    p.compensation.salary_floor = 1000000
    p.compensation.currency = "INR"
    p.compensation.period = "year"

    assert any("floor" in r for r in hard_filter_reasons(posting(salary_max=800000, salary_currency="INR", salary_period="year"), "", p))
    assert any("floor" in r for r in hard_filter_reasons(posting(salary_max=50000, salary_currency="inr", salary_period="month"), "", p))
    assert hard_filter_reasons(posting(salary_max=100000, salary_currency="INR", salary_period="month"), "", p) == []
    assert hard_filter_reasons(posting(salary_max=1, salary_currency="USD", salary_period="year"), "", p) == []
    assert hard_filter_reasons(posting(salary_min=800000, salary_currency="INR", salary_period="year"), "", p) == []
    assert hard_filter_reasons(posting(), "", p) == []


def auth_prefs(*entries):
    p = prefs()
    p.work_authorization = [WorkAuthorization(country=c, authorized=a, needs_sponsorship=s) for c, a, s in entries]
    return p


def test_a_country_that_needs_sponsorship_is_rejected_when_the_posting_offers_none():
    p = auth_prefs(("India", True, False), ("US (United States)", True, True))
    job = posting(country="United States", sponsorship="not_offered")

    assert any("sponsorship" in r for r in hard_filter_reasons(job, "", p))
    assert hard_filter_reasons(posting(country="United States", sponsorship="offered"), "", p) == []
    assert hard_filter_reasons(posting(country="United States"), "", p) == []


def test_a_country_with_full_authorization_passes_even_without_sponsorship():
    p = auth_prefs(("India", True, False))

    assert hard_filter_reasons(posting(country="India", sponsorship="not_offered"), "", p) == []


def test_a_country_with_no_authorization_entry_is_rejected_when_no_sponsorship_is_offered():
    p = auth_prefs(("India", True, False))

    assert any("authorization" in r for r in hard_filter_reasons(posting(country="Germany", sponsorship="not_offered"), "", p))
    assert hard_filter_reasons(posting(country="Germany", sponsorship="offered"), "", p) == []


def test_every_failed_filter_is_reported():
    p = prefs()
    p.blacklist.companies = ["Acme"]
    p.target.seniority = ["junior"]

    found = hard_filter_reasons(posting(seniority="senior"), "", p)

    assert len(found) == 2
