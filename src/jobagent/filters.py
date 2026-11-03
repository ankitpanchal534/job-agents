from __future__ import annotations

import re

from jobagent.config import Preferences
from jobagent.jd import JobPosting
from jobagent.text import normalize_question


def _same_place(a: str, b: str) -> bool:
    tokens_a, tokens_b = set(normalize_question(a).split()), set(normalize_question(b).split())
    return bool(tokens_a and tokens_b) and (tokens_a <= tokens_b or tokens_b <= tokens_a)


def _yearly(amount: float, period: str) -> float:
    return amount * 12 if period == "month" else amount


def _blacklist(posting: JobPosting, jd_text: str, prefs: Preferences) -> list[str]:
    found = []
    for name in prefs.blacklist.companies:
        if name.strip() and name.lower() in posting.company.lower():
            found.append(f"company '{posting.company}' is blacklisted ({name})")
    for phrase in prefs.blacklist.description_keywords:
        if phrase.strip() and phrase.lower() in jd_text.lower():
            found.append(f"description mentions blacklisted phrase '{phrase}'")
    return found


def _title(posting: JobPosting, prefs: Preferences) -> list[str]:
    title = normalize_question(posting.title)
    found = []
    roles = [normalize_question(role) for role in prefs.target.roles if role.strip()]
    if roles and not any(role in title for role in roles):
        found.append(f"title '{posting.title}' matches none of your target roles")
    for word in prefs.target.exclude_title_keywords:
        pattern = rf"\b{re.escape(normalize_question(word))}\b"
        if word.strip() and re.search(pattern, title):
            found.append(f"title contains the excluded word '{word}'")
    return found


def _seniority_years_type(posting: JobPosting, prefs: Preferences) -> list[str]:
    found = []
    wanted = prefs.target.seniority
    if wanted and posting.seniority and posting.seniority not in wanted:
        found.append(f"seniority '{posting.seniority}' is not in your list {wanted}")
    years = prefs.target.years_experience
    if posting.years_required_min is not None and posting.years_required_min > years.max:
        found.append(f"asks {posting.years_required_min:g}+ years, above your limit of {years.max}")
    if posting.years_required_max is not None and posting.years_required_max < years.min:
        found.append(f"asks at most {posting.years_required_max:g} years, below your minimum of {years.min}")
    types = prefs.target.job_types
    if types and posting.job_type and posting.job_type not in types:
        found.append(f"job type '{posting.job_type}' is not in your list {types}")
    return found


def _location(posting: JobPosting, prefs: Preferences) -> list[str]:
    mode = prefs.locations.remote
    if mode == "only" and posting.remote in ("onsite", "hybrid"):
        return [f"you want remote only but the job is {posting.remote}"]
    if mode == "no" and posting.remote == "remote":
        return ["you want on-site only but the job is remote"]
    if posting.remote == "remote":
        return []

    verdicts = []
    if posting.country and prefs.locations.countries:
        verdicts.append(any(_same_place(posting.country, c) for c in prefs.locations.countries))
    if posting.city and prefs.locations.cities:
        verdicts.append(any(_same_place(posting.city, c) for c in prefs.locations.cities))
    if verdicts and not any(verdicts):
        place = ", ".join(part for part in (posting.city, posting.country) if part)
        return [f"location {place} is outside your countries and cities"]
    return []


def _salary(posting: JobPosting, prefs: Preferences) -> list[str]:
    pay = prefs.compensation
    if pay.salary_floor <= 0 or posting.salary_max is None or not posting.salary_period:
        return []
    if not posting.salary_currency or posting.salary_currency.lower() != pay.currency.lower():
        return []
    if _yearly(posting.salary_max, posting.salary_period) < _yearly(pay.salary_floor, pay.period):
        return [
            f"pays up to {posting.salary_max:g} {pay.currency} per {posting.salary_period}, "
            f"below your floor of {pay.salary_floor:g} per {pay.period}"
        ]
    return []


def _authorization(posting: JobPosting, prefs: Preferences) -> list[str]:
    country = posting.country
    if not country or posting.sponsorship != "not_offered":
        return []
    entry = next(
        (e for e in prefs.work_authorization if e.country.strip() and _same_place(country, e.country)), None
    )
    if entry is None:
        return [f"you have no work authorization for {country} and the posting offers no sponsorship"]
    if entry.needs_sponsorship or not entry.authorized:
        return [f"you would need sponsorship in {country} and the posting offers none"]
    return []


def hard_filter_reasons(posting: JobPosting, jd_text: str, prefs: Preferences) -> list[str]:
    return (
        _blacklist(posting, jd_text, prefs)
        + _title(posting, prefs)
        + _seniority_years_type(posting, prefs)
        + _location(posting, prefs)
        + _salary(posting, prefs)
        + _authorization(posting, prefs)
    )
