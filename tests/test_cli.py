import shutil
from pathlib import Path

import yaml
from fakes import FakeChannel, FakeLLM
from matching import JD_TEXT, URL, make_llm

from jobagent.cli import main
from jobagent.fetch import FetchError
from jobagent.llm import LLMError
from jobagent.storage import Storage

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SETTINGS = FIXTURES / "settings.yaml"
PREFERENCES = FIXTURES / "preferences.yaml"


PROFILE = FIXTURES / "profile"


def args(**overrides):
    values = {"settings": SETTINGS, "preferences": PREFERENCES, "profile-dir": PROFILE, **overrides}
    return ["check-config", *[part for key, path in values.items() for part in (f"--{key}", str(path))]]


def run(capsys):
    code = main(args())
    return code, capsys.readouterr().out


def test_check_config_includes_profile_warnings(capsys):
    code, out = run(capsys)
    assert code == 0
    assert "personal.full_name" in out
    assert "skills.yaml" in out


def test_check_config_exits_one_when_a_profile_file_is_missing(tmp_path, capsys):
    empty = tmp_path / "profile"
    empty.mkdir()

    code = main(args(**{"profile-dir": empty}))

    assert code == 1
    assert "personal.yaml" in capsys.readouterr().out


def test_check_config_prints_provider_models_and_warnings_and_exits_zero(capsys):
    code, out = run(capsys)
    assert code == 0
    assert "provider: local" in out
    assert "jd_extraction" in out and "qwen2.5:7b" in out
    assert "target.roles" in out


def test_check_config_exits_one_and_names_the_problem_for_invalid_settings(tmp_path, capsys):
    data = yaml.safe_load(SETTINGS.read_text(encoding="utf-8"))
    data["llm"]["provider"] = "gemini"
    bad = tmp_path / "settings.yaml"
    bad.write_text(yaml.safe_dump(data), encoding="utf-8")

    code = main(["check-config", "--settings", str(bad), "--preferences", str(PREFERENCES)])

    assert code == 1
    assert "gemini" in capsys.readouterr().out


def test_check_config_exits_one_when_a_file_is_missing(tmp_path, capsys):
    missing = tmp_path / "nope.yaml"

    code = main(["check-config", "--settings", str(missing), "--preferences", str(PREFERENCES)])

    assert code == 1
    assert "nope.yaml" in capsys.readouterr().out


def answer_setup(tmp_path):
    profile = tmp_path / "profile"
    shutil.copytree(PROFILE, profile, dirs_exist_ok=True)
    (profile / "personal.yaml").write_text(
        yaml.safe_dump({"full_name": "Asha Rao", "email": "asha@example.com"}), encoding="utf-8"
    )
    return profile, tmp_path / "jobs.db"


def answer(tmp_path, question, llm, *extra):
    profile, db = answer_setup(tmp_path)
    argv = [
        "answer", question,
        "--settings", str(SETTINGS),
        "--preferences", str(PREFERENCES),
        "--profile-dir", str(profile),
        "--db", str(db),
        *extra,
    ]
    return main(argv, llm=llm), Storage(db)


def test_answer_prints_a_profile_field_and_queues_nothing(tmp_path, capsys):
    llm = FakeLLM()

    code, storage = answer(tmp_path, "Email address", llm)

    out = capsys.readouterr().out
    assert code == 0
    assert "asha@example.com" in out and "field" in out
    assert storage.list_answers() == []
    assert llm.calls == []


def test_answer_queues_a_draft_for_review(tmp_path, capsys):
    code, storage = answer(tmp_path, "Why do you want to work here?", FakeLLM(answer_generation="A draft."))

    out = capsys.readouterr().out
    assert code == 0
    assert "A draft." in out and "review" in out
    assert [(a.status, a.answer) for a in storage.list_answers()] == [("pending_review", "A draft.")]


def test_answer_blocks_and_queues_a_sensitive_question_for_input(tmp_path, capsys):
    llm = FakeLLM()

    code, storage = answer(tmp_path, "What are your salary expectations?", llm)

    out = capsys.readouterr().out
    assert code == 2
    assert "BLOCKED" in out and "compensation" in out
    assert [(a.status, a.answer) for a in storage.list_answers()] == [("needs_input", None)]
    assert llm.calls == []


def test_answer_uses_answers_approved_earlier_in_the_review_page(tmp_path, capsys):
    _, db = answer_setup(tmp_path)
    storage = Storage(db)
    storage.approve_answer(storage.queue_answer("Why this company?", answer="x").id, final_answer="Because.")
    llm = FakeLLM()

    code, _ = answer(tmp_path, "Why this company?", llm)

    assert code == 0
    assert "Because." in capsys.readouterr().out
    assert llm.calls == []


def test_answer_reports_an_unreachable_model_and_exits_one(tmp_path, capsys):
    code, storage = answer(tmp_path, "Why do you want to work here?", FakeLLM(answer_generation=LLMError("cannot reach it")))

    assert code == 1
    assert "cannot reach it" in capsys.readouterr().out
    assert storage.list_answers() == []


def test_llm_test_prints_the_resolved_model_and_the_reply(capsys):
    llm = FakeLLM(jd_summary="OK")

    code = main(["llm-test", "--settings", str(SETTINGS)], llm=llm)

    out = capsys.readouterr().out
    assert code == 0
    assert "local / qwen2.5:7b" in out and "OK" in out
    assert llm.tasks == ["jd_summary"]


def test_llm_test_reports_a_failing_model_and_exits_one(capsys):
    code = main(["llm-test", "--settings", str(SETTINGS)], llm=FakeLLM(jd_summary=LLMError("cannot reach it")))

    assert code == 1
    assert "cannot reach it" in capsys.readouterr().out


def test_llm_test_rejects_an_unknown_task(capsys):
    code = main(["llm-test", "--task", "nope", "--settings", str(SETTINGS)], llm=FakeLLM())

    assert code == 1
    assert "nope" in capsys.readouterr().out


def test_models_lists_the_active_providers_models(capsys):
    llm = FakeLLM(model_list=["m1", "m2"])

    code = main(["models", "--settings", str(SETTINGS)], llm=llm)

    assert code == 0
    assert capsys.readouterr().out.split() == ["m1", "m2"]
    assert llm.listed_provider == "local"


def test_models_can_target_a_named_provider_and_rejects_unknown_ones(capsys):
    llm = FakeLLM(model_list=["m1"])
    assert main(["models", "--provider", "openai", "--settings", str(SETTINGS)], llm=llm) == 0
    assert llm.listed_provider == "openai"

    assert main(["models", "--provider", "huggingface", "--settings", str(SETTINGS)], llm=llm) == 1
    assert "huggingface" in capsys.readouterr().out


def match(tmp_path, llm, *extra, url=URL, text_file=True):
    jd = tmp_path / "jd.txt"
    jd.write_text(JD_TEXT, encoding="utf-8")
    argv = [
        "match", url,
        "--settings", str(SETTINGS),
        "--preferences", str(PREFERENCES),
        "--profile-dir", str(PROFILE),
        "--db", str(tmp_path / "jobs.db"),
        *(["--text-file", str(jd)] if text_file else []),
        *extra,
    ]
    return main(argv, llm=llm), Storage(tmp_path / "jobs.db")


def test_match_evaluates_a_job_and_saves_it_to_the_tracker(tmp_path, capsys):
    code, storage = match(tmp_path, make_llm())

    out = capsys.readouterr().out
    assert code == 0
    assert "DECISION: apply (score 85.7)" in out
    assert "A frontend role at Acme." in out and "queued" in out
    assert [(j.status, j.score) for j in storage.list_jobs()] == [("queued", 85.7)]


def test_match_skips_a_job_it_has_already_evaluated_unless_forced(tmp_path, capsys):
    llm = make_llm()
    match(tmp_path, llm)
    capsys.readouterr()

    code, storage = match(tmp_path, llm)
    skipped = capsys.readouterr().out
    assert code == 0 and "SKIPPED" in skipped and "--force" in skipped
    assert len(storage.list_evaluations(storage.list_jobs()[0].id)) == 1

    code, storage = match(tmp_path, llm, "--force")
    assert code == 0 and "SKIPPED" not in capsys.readouterr().out
    assert len(storage.list_evaluations(storage.list_jobs()[0].id)) == 2


def test_match_reports_a_model_failure_and_exits_one(tmp_path, capsys):
    code, _ = match(tmp_path, FakeLLM(jd_extraction=LLMError("cannot reach it")))

    assert code == 1
    assert "cannot reach it" in capsys.readouterr().out


def test_match_reports_an_unreadable_model_reply_and_exits_one(tmp_path, capsys):
    code, storage = match(tmp_path, FakeLLM(jd_extraction="nonsense"))

    assert code == 1
    assert "no JSON" in capsys.readouterr().out
    assert [j.status for j in storage.list_jobs()] == ["new"]


def test_match_fetches_the_page_when_no_text_file_is_given(tmp_path, capsys, monkeypatch):
    seen = []
    monkeypatch.setattr("jobagent.cli.fetch_jd_text", lambda url: seen.append(url) or JD_TEXT)

    code, _ = match(tmp_path, make_llm(), text_file=False)

    assert code == 0 and seen == [URL]


def test_match_reports_a_failed_fetch_and_exits_one(tmp_path, capsys, monkeypatch):
    def failing(url):
        raise FetchError("the page has no readable text")

    monkeypatch.setattr("jobagent.cli.fetch_jd_text", failing)

    code, storage = match(tmp_path, make_llm(), text_file=False)

    assert code == 1
    assert "no readable text" in capsys.readouterr().out
    assert storage.list_jobs() == []


def test_match_reports_a_missing_text_file_and_exits_one(tmp_path, capsys):
    code, _ = match(tmp_path, make_llm(), "--text-file", str(tmp_path / "gone.txt"))

    assert code == 1
    assert "gone.txt" in capsys.readouterr().out


def seeded_db(tmp_path, count=1, host="boards.greenhouse.io"):
    storage = Storage(tmp_path / "jobs.db")
    for n in range(1, count + 1):
        job = storage.add_job(f"https://{host}/acme/jobs/{n}")
        storage.update_job(job.id, title="Frontend Engineer", company="Acme")
        storage.set_status(job.id, "queued")
    return storage


def block(tmp_path, job_id, channels, *extra, reason="captcha"):
    argv = ["block", str(job_id), "--reason", reason, "--settings", str(SETTINGS), "--db", str(tmp_path / "jobs.db"), *extra]
    return main(argv, channels=channels)


def test_notify_test_sends_a_test_alert_to_every_channel(capsys):
    working = FakeChannel("working")

    code = main(["notify-test", "--settings", str(SETTINGS)], channels=[working])

    assert code == 0
    assert "ok    working" in capsys.readouterr().out
    assert working.sent[0].title == "jobagent test"


def test_notify_test_names_the_channel_that_failed_and_exits_one(capsys):
    code = main(["notify-test", "--settings", str(SETTINGS)], channels=[FakeChannel("a"), FakeChannel("b", fail=True)])

    out = capsys.readouterr().out
    assert code == 1
    assert "ok    a" in out and "FAILED b" in out and "b is down" in out


def test_notify_test_with_no_channels_says_so_and_exits_one(capsys):
    code = main(["notify-test", "--settings", str(SETTINGS)], channels=[])

    assert code == 1
    assert "No notification channels" in capsys.readouterr().out


def test_notify_test_prints_the_notes_about_skipped_channels(capsys, monkeypatch):
    monkeypatch.setattr("jobagent.cli.build_channels", lambda settings: ([FakeChannel()], ["ntfy is skipped: placeholder"]))

    main(["notify-test", "--settings", str(SETTINGS)])

    assert "ntfy is skipped: placeholder" in capsys.readouterr().out


def test_block_marks_the_job_blocked_and_sends_an_alert(tmp_path, capsys):
    storage = seeded_db(tmp_path)
    channel = FakeChannel()

    code = block(tmp_path, 1, [channel], "--detail", "Cloudflare check")

    out = capsys.readouterr().out
    assert code == 0
    assert "Job #1 blocked" in out and "Alert sent via fake" in out
    assert storage.get_job(1).status == "blocked" and len(channel.sent) == 1


def test_a_second_block_on_the_same_site_holds_the_alert_back(tmp_path, capsys):
    seeded_db(tmp_path, count=2)
    channel = FakeChannel()
    block(tmp_path, 1, [channel])
    capsys.readouterr()

    code = block(tmp_path, 2, [channel])

    assert code == 0
    assert "suppressed" in capsys.readouterr().out
    assert len(channel.sent) == 1


def test_block_records_the_block_even_when_every_channel_fails(tmp_path, capsys):
    storage = seeded_db(tmp_path)

    code = block(tmp_path, 1, [FakeChannel("bad", fail=True)])

    assert code == 0
    assert "FAILED bad" in capsys.readouterr().out
    assert storage.get_job(1).status == "blocked"


def test_block_rejects_an_unknown_job(tmp_path, capsys):
    seeded_db(tmp_path)

    code = block(tmp_path, 99, [FakeChannel()])

    assert code == 1
    assert "no job" in capsys.readouterr().out


def test_blocks_lists_the_open_blocks_and_says_when_there_are_none(tmp_path, capsys):
    seeded_db(tmp_path)
    main(["blocks", "--settings", str(SETTINGS), "--db", str(tmp_path / "jobs.db")])
    assert "No open blocks" in capsys.readouterr().out

    block(tmp_path, 1, [FakeChannel()], "--detail", "Cloudflare check")
    capsys.readouterr()
    main(["blocks", "--settings", str(SETTINGS), "--db", str(tmp_path / "jobs.db")])

    out = capsys.readouterr().out
    assert "job #1" in out and "captcha" in out and "Cloudflare check" in out and "boards.greenhouse.io" in out


def test_resume_puts_a_blocked_job_back_in_the_apply_queue(tmp_path, capsys):
    storage = seeded_db(tmp_path)
    block(tmp_path, 1, [FakeChannel()])
    capsys.readouterr()

    code = main(["resume", "1", "--settings", str(SETTINGS), "--db", str(tmp_path / "jobs.db")])

    assert code == 0
    assert "back in the apply queue" in capsys.readouterr().out
    assert storage.get_job(1).status == "queued" and storage.list_blocks() == []


def test_resume_rejects_a_job_that_is_not_blocked(tmp_path, capsys):
    seeded_db(tmp_path)

    code = main(["resume", "1", "--settings", str(SETTINGS), "--db", str(tmp_path / "jobs.db")])

    assert code == 1
    assert "not blocked" in capsys.readouterr().out


BATCH_URLS = ["https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/2"]


def batch(tmp_path, llm, lines, *extra):
    urls_file = tmp_path / "urls.txt"
    urls_file.write_text("\n".join(lines), encoding="utf-8")
    argv = [
        "batch", str(urls_file),
        "--settings", str(SETTINGS),
        "--preferences", str(PREFERENCES),
        "--profile-dir", str(PROFILE),
        "--db", str(tmp_path / "jobs.db"),
        *extra,
    ]
    return main(argv, llm=llm), Storage(tmp_path / "jobs.db")


def test_batch_evaluates_every_address_in_the_file_and_summarises(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("jobagent.cli.fetch_jd_text", lambda url: JD_TEXT)

    code, storage = batch(tmp_path, make_llm(), ["# my list", *BATCH_URLS])

    out = capsys.readouterr().out
    assert code == 0
    assert "Done: 2 apply, 0 watch_later, 0 reject, 0 skipped, 0 errors." in out
    assert [j.status for j in storage.list_jobs()] == ["queued", "queued"]


def test_batch_exits_one_when_any_job_failed_but_still_does_the_rest(tmp_path, capsys, monkeypatch):
    def fetch(url):
        if url.endswith("/1"):
            raise FetchError("the page has no readable text")
        return JD_TEXT

    monkeypatch.setattr("jobagent.cli.fetch_jd_text", fetch)

    code, storage = batch(tmp_path, make_llm(), BATCH_URLS)

    out = capsys.readouterr().out
    assert code == 1
    assert "1 apply" in out and "1 errors" in out
    assert [j.status for j in storage.list_jobs()] == ["queued"]


def test_batch_stops_and_exits_one_when_the_model_cannot_be_used(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("jobagent.cli.fetch_jd_text", lambda url: JD_TEXT)

    code, _ = batch(tmp_path, FakeLLM(jd_extraction=LLMError("huggingface returned HTTP 402")), BATCH_URLS)

    out = capsys.readouterr().out
    assert code == 1
    assert "Stopping" in out and "[2/2]" not in out


def test_batch_reports_lines_that_are_not_web_addresses_as_errors(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("jobagent.cli.fetch_jd_text", lambda url: JD_TEXT)

    code, _ = batch(tmp_path, make_llm(), [BATCH_URLS[0], "not-a-link"])

    out = capsys.readouterr().out
    assert code == 1
    assert "not-a-link" in out and "not a web address" in out and "1 apply" in out and "1 errors" in out


def test_batch_reports_a_missing_file_and_an_empty_one(tmp_path, capsys):
    missing = ["batch", str(tmp_path / "nope.txt"), "--settings", str(SETTINGS), "--preferences", str(PREFERENCES), "--profile-dir", str(PROFILE)]
    assert main(missing, llm=make_llm()) == 1
    assert "nope.txt" in capsys.readouterr().out

    code, _ = batch(tmp_path, make_llm(), ["# nothing here"])
    assert code == 0 and "No web addresses" in capsys.readouterr().out


def test_batch_skips_tracked_links_unless_forced(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("jobagent.cli.fetch_jd_text", lambda url: JD_TEXT)
    batch(tmp_path, make_llm(), BATCH_URLS)
    capsys.readouterr()

    batch(tmp_path, make_llm(), BATCH_URLS)
    assert "2 skipped" in capsys.readouterr().out

    batch(tmp_path, make_llm(), BATCH_URLS, "--force")
    assert "2 apply" in capsys.readouterr().out
