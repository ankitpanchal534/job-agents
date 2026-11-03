from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = ROOT / "config" / "settings.yaml"
DEFAULT_PREFERENCES = ROOT / "profile" / "preferences.yaml"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Provider(StrictModel):
    base_url: str
    api_key_env: Optional[str] = None


class Task(StrictModel):
    what: str = ""
    models: dict[str, str]
    provider: Optional[str] = None
    temperature: float = 0.0
    max_tokens: int = Field(default=1000, gt=0)


class LLM(StrictModel):
    provider: str
    providers: dict[str, Provider]
    tasks: dict[str, Task]

    @model_validator(mode="after")
    def _check_providers_and_models(self) -> "LLM":
        known = sorted(self.providers)
        if self.provider not in self.providers:
            raise ValueError(f"llm.provider '{self.provider}' is not one of {known}")
        for name, task in self.tasks.items():
            if task.provider is not None and task.provider not in self.providers:
                raise ValueError(f"task '{name}' provider '{task.provider}' is not one of {known}")
            unknown = sorted(set(task.models) - set(self.providers))
            if unknown:
                raise ValueError(f"task '{name}' lists models for unknown provider(s): {unknown}")
            effective = task.provider or self.provider
            if effective not in task.models:
                raise ValueError(f"task '{name}' has no model for provider '{effective}'")
        return self


class Matcher(StrictModel):
    apply_threshold: int = Field(default=75, ge=0, le=100)
    watch_floor: int = Field(default=50, ge=0, le=100)
    must_have_weight: float = Field(default=3, gt=0)
    nice_to_have_weight: float = Field(default=1, gt=0)

    @model_validator(mode="after")
    def _floor_not_above_threshold(self) -> "Matcher":
        if self.watch_floor > self.apply_threshold:
            raise ValueError("matcher.watch_floor must not be above matcher.apply_threshold")
        return self


class ResolverSettings(StrictModel):
    fuzzy_match_cutoff: float = Field(default=0.9, ge=0, le=1)
    llm_match_confidence: float = Field(default=0.8, ge=0, le=1)
    llm_match_candidates: int = Field(default=5, ge=1)


class Ntfy(StrictModel):
    enabled: bool = True
    server: str = "https://ntfy.sh"
    topic: str


class Desktop(StrictModel):
    toast: bool = True
    sound: bool = True


class Notifications(StrictModel):
    ntfy: Ntfy
    desktop: Desktop = Desktop()
    cooldown_minutes: float = Field(default=15, ge=0)


class ReviewUI(StrictModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8501, gt=0, lt=65536)


class Storage(StrictModel):
    db_path: str = "data/jobs.db"


class Guardrails(StrictModel):
    allowed_domains: list[str] = []


class Settings(StrictModel):
    llm: LLM
    matcher: Matcher = Matcher()
    resolver: ResolverSettings = ResolverSettings()
    notifications: Notifications
    review_ui: ReviewUI = ReviewUI()
    storage: Storage = Storage()
    guardrails: Guardrails = Guardrails()


Seniority = Literal["intern", "junior", "mid", "senior", "lead", "principal", "manager"]
JobType = Literal["full-time", "part-time", "contract", "internship"]


class YearsRange(StrictModel):
    min: int = Field(default=0, ge=0)
    max: int = Field(default=40, ge=0)

    @model_validator(mode="after")
    def _min_not_above_max(self) -> "YearsRange":
        if self.min > self.max:
            raise ValueError("years_experience.min must not be above years_experience.max")
        return self


class Target(StrictModel):
    roles: list[str] = []
    exclude_title_keywords: list[str] = []
    seniority: list[Seniority] = []
    years_experience: YearsRange = YearsRange()
    job_types: list[JobType] = ["full-time"]


class Locations(StrictModel):
    countries: list[str] = []
    cities: list[str] = []
    remote: Literal["only", "preferred", "allowed", "no"] = "allowed"
    open_to_relocation: bool = False


class Compensation(StrictModel):
    salary_floor: float = Field(default=0, ge=0)
    currency: str = "INR"
    period: Literal["year", "month"] = "year"


class WorkAuthorization(StrictModel):
    country: str
    authorized: bool = True
    needs_sponsorship: bool = False


class Availability(StrictModel):
    notice_period_days: int = Field(default=30, ge=0)


class Blacklist(StrictModel):
    companies: list[str] = []
    description_keywords: list[str] = []


class Preferences(StrictModel):
    target: Target = Target()
    locations: Locations = Locations()
    compensation: Compensation = Compensation()
    work_authorization: list[WorkAuthorization] = []
    availability: Availability = Availability()
    blacklist: Blacklist = Blacklist()


@dataclass(frozen=True)
class ResolvedTask:
    provider: str
    base_url: str
    api_key_env: Optional[str]
    model: str
    temperature: float
    max_tokens: int


def read_yaml(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def load_settings(path: Path = DEFAULT_SETTINGS) -> Settings:
    return Settings.model_validate(read_yaml(path))


def load_preferences(path: Path = DEFAULT_PREFERENCES) -> Preferences:
    return Preferences.model_validate(read_yaml(path))


def load_env(path: Path = ROOT / ".env", environ: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    values: dict[str, str] = {}
    path = Path(path)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and value:
                values[key] = value
    return {**values, **environ}


def db_path(settings: Settings) -> Path:
    path = Path(settings.storage.db_path)
    return path if path.is_absolute() else ROOT / path


def resolve_task(settings: Settings, task_name: str) -> ResolvedTask:
    task = settings.llm.tasks[task_name]
    provider_name = task.provider or settings.llm.provider
    provider = settings.llm.providers[provider_name]
    return ResolvedTask(
        provider=provider_name,
        base_url=provider.base_url,
        api_key_env=provider.api_key_env,
        model=task.models[provider_name],
        temperature=task.temperature,
        max_tokens=task.max_tokens,
    )


def check_config(
    settings: Settings,
    prefs: Preferences,
    env: Optional[Mapping[str, str]] = None,
) -> list[str]:
    env = os.environ if env is None else env
    warnings: list[str] = []

    if not prefs.target.roles:
        warnings.append("target.roles is empty: no job title will match until you add some.")
    if settings.notifications.ntfy.enabled and settings.notifications.ntfy.topic.startswith("CHANGE-ME"):
        warnings.append("notifications.ntfy.topic is still the placeholder: set a long random string.")
    if not settings.guardrails.allowed_domains:
        warnings.append("guardrails.allowed_domains is empty: the applicant will be allowed no site.")

    for provider in settings.llm.providers:
        lacking = [name for name, task in settings.llm.tasks.items() if provider not in task.models]
        if lacking:
            warnings.append(
                f"provider '{provider}' has no model for task(s) {lacking}: "
                f"switching llm.provider to '{provider}' will fail until you add them."
            )

    needed_keys = {
        resolved.api_key_env
        for resolved in (resolve_task(settings, name) for name in settings.llm.tasks)
        if resolved.api_key_env
    }
    for key in sorted(needed_keys):
        if not env.get(key):
            warnings.append(f"{key} is not set but a task uses it: add it to .env.")

    return warnings
