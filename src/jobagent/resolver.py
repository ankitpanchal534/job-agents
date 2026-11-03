from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol

from jobagent.config import Preferences, Settings
from jobagent.profile import Profile
from jobagent.text import normalize_question

SENSITIVE_PATTERNS = {
    "work_authorization": [
        r"\bauthori[sz]ed to work", r"\bwork authori[sz]ation", r"\blegally authori[sz]ed", r"\bright to work",
        r"\bvisa\b", r"\bsponsor", r"\bwork permit", r"\bcitizen", r"\bimmigration",
    ],
    "compensation": [
        r"\bsalary", r"\bcompensation", r"\bctc\b", r"\bpay expectation", r"\bexpected pay",
        r"\bremuneration", r"\bcurrent pay",
    ],
    "notice_period": [
        r"\bnotice period", r"\bwhen can you start", r"\bearliest start", r"\bstart date", r"\bhow soon can you join",
    ],
    "relocation": [r"\brelocat"],
    "background": [r"\bcriminal", r"\bconvict", r"\bbackground check", r"\bfelony", r"\bmisdemeanor", r"\barrest"],
    "disability": [r"\bdisabilit", r"\bdisabled\b", r"\baccommodation"],
    "demographic": [
        r"\bgender", r"\brace\b", r"\bethnic", r"\bveteran", r"\bsexual orientation", r"\breligio",
        r"\bmarital", r"\bdate of birth", r"\bcaste\b", r"\bnationality", r"\bpronoun",
    ],
    "references": [r"\breferences?\b"],
}

LABEL_STOPWORDS = {
    "please", "enter", "provide", "your", "the", "a", "an", "what", "is", "are", "do", "you", "have",
    "state", "give", "tell", "me", "us",
}

INSUFFICIENT = "INSUFFICIENT"


class LLM(Protocol):
    def complete(self, task: str, system: str, user: str) -> str: ...


@dataclass(frozen=True)
class Resolution:
    answer: Optional[str] = None
    source: Optional[str] = None
    needs_review: bool = False
    blocked_reason: Optional[str] = None


def classify_sensitive(normalized_question: str) -> Optional[str]:
    for category, patterns in SENSITIVE_PATTERNS.items():
        if any(re.search(pattern, normalized_question) for pattern in patterns):
            return category
    return None


def _label(question: str) -> str:
    return " ".join(word for word in normalize_question(question).split() if word not in LABEL_STOPWORDS)


class Resolver:
    def __init__(
        self,
        profile: Profile,
        prefs: Preferences,
        settings: Settings,
        llm: LLM,
        extra_bank: Iterable[tuple[str, str]] = (),
    ):
        self._profile = profile
        self._prefs = prefs
        self._cfg = settings.resolver
        self._llm = llm
        entries = [(entry.question, entry.answer) for entry in profile.answers.faq] + list(extra_bank)
        self._bank = [(normalize_question(question), question, answer) for question, answer in entries]
        self._fields = self._build_field_table()

    def resolve(self, question: str, max_length: Optional[int] = None, jd_summary: str = "") -> Resolution:
        key = normalize_question(question)
        category = classify_sensitive(key)

        for bank_key, _, answer in self._bank:
            if bank_key == key:
                return Resolution(answer=answer, source="bank")

        if category is None:
            fuzzy = self._fuzzy_match(key)
            if fuzzy is not None:
                return Resolution(answer=fuzzy, source="bank")
            field = self._field_answer(question)
            if field:
                return Resolution(answer=field, source="field")

        preference = self._preference_answer(category)
        if preference:
            return Resolution(answer=preference, source="preference")

        if category is not None:
            return Resolution(
                blocked_reason=f"{category} questions are never drafted: add a curated answer to answers.yaml"
            )

        matched = self._llm_match(question, key)
        if matched is not None:
            return Resolution(answer=matched, source="bank")

        return self._draft(question, max_length, jd_summary)

    def _build_field_table(self) -> dict[str, Callable[[], str]]:
        personal = self._profile.personal
        current = next((role for role in self._profile.experience.roles if role.end == "present"), None)
        names = personal.full_name.split()
        table = {
            "name": lambda: personal.full_name,
            "full name": lambda: personal.full_name,
            "legal name": lambda: personal.full_name,
            "first name": lambda: names[0] if names else "",
            "given name": lambda: names[0] if names else "",
            "last name": lambda: names[-1] if len(names) > 1 else "",
            "surname": lambda: names[-1] if len(names) > 1 else "",
            "email": lambda: personal.email,
            "email address": lambda: personal.email,
            "e mail": lambda: personal.email,
            "phone": lambda: personal.phone,
            "phone number": lambda: personal.phone,
            "mobile": lambda: personal.phone,
            "mobile number": lambda: personal.phone,
            "city": lambda: personal.city,
            "current city": lambda: personal.city,
            "country": lambda: personal.country,
            "country of residence": lambda: personal.country,
            "linkedin": lambda: personal.links.linkedin,
            "linkedin url": lambda: personal.links.linkedin,
            "linkedin profile": lambda: personal.links.linkedin,
            "linkedin profile url": lambda: personal.links.linkedin,
            "github": lambda: personal.links.github,
            "github url": lambda: personal.links.github,
            "portfolio": lambda: personal.links.portfolio,
            "portfolio url": lambda: personal.links.portfolio,
            "website": lambda: personal.links.portfolio,
            "current company": lambda: current.company if current else "",
            "current employer": lambda: current.company if current else "",
            "current title": lambda: current.title if current else "",
            "current job title": lambda: current.title if current else "",
            "current role": lambda: current.title if current else "",
        }
        return table

    def _field_answer(self, question: str) -> Optional[str]:
        getter = self._fields.get(_label(question))
        return getter() or None if getter else None

    def _preference_answer(self, category: Optional[str]) -> Optional[str]:
        if category == "notice_period":
            return f"{self._prefs.availability.notice_period_days} days"
        if category == "relocation":
            return "Yes" if self._prefs.locations.open_to_relocation else "No"
        return None

    def _fuzzy_match(self, key: str) -> Optional[str]:
        best_ratio, best_answer = 0.0, None
        for bank_key, _, answer in self._bank:
            ratio = difflib.SequenceMatcher(None, key, bank_key).ratio()
            if ratio > best_ratio:
                best_ratio, best_answer = ratio, answer
        return best_answer if best_ratio >= self._cfg.fuzzy_match_cutoff else None

    def _llm_match(self, question: str, key: str) -> Optional[str]:
        if not self._bank:
            return None
        ranked = sorted(
            self._bank,
            key=lambda entry: difflib.SequenceMatcher(None, key, entry[0]).ratio(),
            reverse=True,
        )[: self._cfg.llm_match_candidates]
        listing = "\n".join(f"{index}: {original}" for index, (_, original, _) in enumerate(ranked))
        reply = self._llm.complete(
            "answer_matching",
            "You match a job application question to the closest curated question. Reply with JSON only: "
            '{"index": <number or null>, "confidence": <0 to 1>}. Use null when no candidate asks for the same thing.',
            f"Question: {question}\n\nCandidates:\n{listing}",
        )
        found = re.search(r"\{.*\}", reply, re.DOTALL)
        if not found:
            return None
        try:
            data = json.loads(found.group(0))
            index, confidence = data["index"], float(data["confidence"])
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(index, int) or not 0 <= index < len(ranked):
            return None
        return ranked[index][2] if confidence >= self._cfg.llm_match_confidence else None

    def _draft(self, question: str, max_length: Optional[int], jd_summary: str) -> Resolution:
        limit = f" Keep the answer under {max_length} characters." if max_length else ""
        reply = self._llm.complete(
            "answer_generation",
            "You write answers to job application questions for the candidate described below. "
            "Use only facts that appear in the profile. Do not invent employers, dates, numbers or skills. "
            "Write in the first person as plain text with no markdown. "
            f"If the profile does not contain enough facts to answer, reply with exactly {INSUFFICIENT}.{limit}",
            f"Candidate profile:\n{self._profile_text()}\n\nJob summary:\n{jd_summary or '(none)'}\n\nQuestion: {question}",
        )
        text = reply.strip()
        if not text or text.rstrip(".").strip().upper() == INSUFFICIENT:
            return Resolution(blocked_reason="the profile does not contain enough facts to answer this")
        if max_length and len(text) > max_length:
            return Resolution(blocked_reason=f"the draft is {len(text)} characters, over the {max_length} limit")
        return Resolution(answer=text, source="generated", needs_review=True)

    def _profile_text(self) -> str:
        lines = [f"Name: {self._profile.personal.full_name}"]
        for role in self._profile.experience.roles:
            lines.append(f"Role: {role.title} at {role.company} ({role.start} to {role.end})")
            summary = role.summary_medium or role.summary_long or role.summary_short
            if summary:
                lines.append(f"  {summary}")
            lines.extend(f"  Achievement: {item}" for item in role.achievements)
            if role.tech:
                lines.append(f"  Tech: {', '.join(role.tech)}")
        for skill in self._profile.skills.skills:
            lines.append(f"Skill: {skill.name}, {skill.years} years, {skill.level}")
        for degree in self._profile.education.degrees:
            lines.append(f"Education: {degree.degree} {degree.field}, {degree.institution}")
        return "\n".join(lines)
