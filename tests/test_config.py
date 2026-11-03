from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from jobagent.config import (
    check_config,
    db_path,
    load_env,
    load_preferences,
    load_settings,
    resolve_task,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SETTINGS = FIXTURES / "settings.yaml"
PREFERENCES = FIXTURES / "preferences.yaml"


def test_real_config_files_are_valid():
    load_settings(ROOT / "config" / "settings.yaml")
    load_preferences(ROOT / "profile" / "preferences.yaml")


def settings_dict():
    return yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))


def write_settings(tmp_path, data):
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_shipped_settings_load_with_local_provider():
    settings = load_settings(SETTINGS)
    assert settings.llm.provider == "local"
    assert settings.matcher.apply_threshold == 75
    assert settings.matcher.watch_floor == 50


def test_resolve_task_follows_the_global_toggle(tmp_path):
    data = settings_dict()
    local = resolve_task(load_settings(write_settings(tmp_path, data)), "jd_extraction")
    assert (local.provider, local.model) == ("local", "qwen2.5:7b")

    data["llm"]["provider"] = "openai"
    openai = resolve_task(load_settings(write_settings(tmp_path, data)), "jd_extraction")
    assert (openai.provider, openai.model) == ("openai", "gpt-4o-mini")
    assert openai.base_url == "https://api.openai.com/v1"
    assert openai.api_key_env == "OPENAI_API_KEY"


def test_task_provider_override_beats_the_global_toggle(tmp_path):
    data = settings_dict()
    data["llm"]["tasks"]["vision"]["provider"] = "openai"
    resolved = resolve_task(load_settings(write_settings(tmp_path, data)), "vision")
    assert (resolved.provider, resolved.model) == ("openai", "gpt-4o")
    other = resolve_task(load_settings(write_settings(tmp_path, data)), "jd_summary")
    assert other.provider == "local"


def test_resolver_cutoffs_have_defaults_and_are_validated(tmp_path):
    resolver = load_settings(SETTINGS).resolver
    assert resolver.fuzzy_match_cutoff == 0.9
    assert resolver.llm_match_confidence == 0.8
    assert resolver.llm_match_candidates == 5

    data = settings_dict()
    data["resolver"] = {"fuzzy_match_cutoff": 1.5}
    with pytest.raises(ValidationError, match="fuzzy_match_cutoff"):
        load_settings(write_settings(tmp_path, data))


def test_db_path_is_relative_to_the_project_root_unless_absolute(tmp_path):
    assert db_path(load_settings(SETTINGS)) == ROOT / "data" / "jobs.db"

    data = settings_dict()
    data["storage"] = {"db_path": str(tmp_path / "elsewhere.db")}
    assert db_path(load_settings(write_settings(tmp_path, data))) == tmp_path / "elsewhere.db"


def test_unknown_task_is_rejected():
    with pytest.raises(KeyError, match="no_such_task"):
        resolve_task(load_settings(SETTINGS), "no_such_task")


def test_task_missing_a_model_for_the_active_provider_is_rejected(tmp_path):
    data = settings_dict()
    del data["llm"]["tasks"]["vision"]["models"]["local"]
    with pytest.raises(ValidationError, match="vision"):
        load_settings(write_settings(tmp_path, data))


def test_task_pinned_to_a_provider_without_a_model_is_rejected(tmp_path):
    data = settings_dict()
    data["llm"]["tasks"]["vision"]["provider"] = "openai"
    del data["llm"]["tasks"]["vision"]["models"]["openai"]
    with pytest.raises(ValidationError, match="vision"):
        load_settings(write_settings(tmp_path, data))


def test_a_model_listed_for_an_unknown_provider_is_rejected(tmp_path):
    data = settings_dict()
    data["llm"]["tasks"]["vision"]["models"]["huggingfce"] = "some/model"
    with pytest.raises(ValidationError, match="huggingfce"):
        load_settings(write_settings(tmp_path, data))


def test_a_provider_without_models_for_every_task_loads_but_check_config_warns(tmp_path):
    data = settings_dict()
    del data["llm"]["tasks"]["vision"]["models"]["openai"]
    settings = load_settings(write_settings(tmp_path, data))

    warnings = check_config(settings, load_preferences(PREFERENCES), env={"OPENAI_API_KEY": "x"})

    gap = [w for w in warnings if "openai" in w and "vision" in w]
    assert len(gap) == 1


def test_load_env_reads_the_dotenv_file_and_the_real_environment_wins(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# a comment\nHF_TOKEN="hf_from_file"\nGROQ_API_KEY=\nexport OTHER=abc\nOPENAI_API_KEY=from_file\n',
        encoding="utf-8",
    )

    env = load_env(env_file, environ={"OPENAI_API_KEY": "from_env"})

    assert env["HF_TOKEN"] == "hf_from_file"
    assert env["OTHER"] == "abc"
    assert env["OPENAI_API_KEY"] == "from_env"
    assert "GROQ_API_KEY" not in env


def test_load_env_without_a_file_returns_just_the_environment(tmp_path):
    assert load_env(tmp_path / "missing.env", environ={"A": "1"}) == {"A": "1"}


def test_unknown_global_provider_is_rejected(tmp_path):
    data = settings_dict()
    data["llm"]["provider"] = "gemini"
    with pytest.raises(ValidationError, match="gemini"):
        load_settings(write_settings(tmp_path, data))


def test_watch_floor_above_apply_threshold_is_rejected(tmp_path):
    data = settings_dict()
    data["matcher"]["watch_floor"] = 90
    with pytest.raises(ValidationError, match="watch_floor"):
        load_settings(write_settings(tmp_path, data))


def test_shipped_preferences_load_with_empty_roles():
    prefs = load_preferences(PREFERENCES)
    assert prefs.target.roles == []
    assert prefs.locations.remote == "allowed"


def test_invalid_remote_value_is_rejected(tmp_path):
    data = yaml.safe_load(PREFERENCES.read_text(encoding="utf-8"))
    data["locations"]["remote"] = "sometimes"
    path = tmp_path / "prefs.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="remote"):
        load_preferences(path)


def test_years_min_above_max_is_rejected(tmp_path):
    data = yaml.safe_load(PREFERENCES.read_text(encoding="utf-8"))
    data["target"]["years_experience"] = {"min": 10, "max": 2}
    path = tmp_path / "prefs.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationError, match="years_experience"):
        load_preferences(path)


def test_check_config_warns_about_unfilled_defaults():
    warnings = check_config(load_settings(SETTINGS), load_preferences(PREFERENCES), env={})
    joined = "\n".join(warnings)
    assert "target.roles" in joined
    assert "ntfy" in joined
    assert "allowed_domains" in joined


def test_check_config_warns_when_active_provider_key_is_missing(tmp_path):
    data = settings_dict()
    data["llm"]["provider"] = "openai"
    settings = load_settings(write_settings(tmp_path, data))
    missing = check_config(settings, load_preferences(PREFERENCES), env={})
    assert any("OPENAI_API_KEY" in w for w in missing)
    present = check_config(settings, load_preferences(PREFERENCES), env={"OPENAI_API_KEY": "x"})
    assert not any("OPENAI_API_KEY" in w for w in present)


def test_notification_cooldown_defaults_to_fifteen_minutes_and_cannot_be_negative(tmp_path):
    assert load_settings(SETTINGS).notifications.cooldown_minutes == 15

    data = settings_dict()
    data["notifications"]["cooldown_minutes"] = -1
    with pytest.raises(ValidationError, match="cooldown_minutes"):
        load_settings(write_settings(tmp_path, data))
