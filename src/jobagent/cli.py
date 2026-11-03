from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Mapping, Optional, Sequence

from pydantic import ValidationError

from jobagent.config import (
    DEFAULT_PREFERENCES,
    DEFAULT_SETTINGS,
    check_config,
    db_path,
    load_env,
    load_preferences,
    load_settings,
    resolve_task,
)
from jobagent.batch import read_urls, run_batch
from jobagent.calibrate import load_labels, run_calibration
from jobagent.fetch import FetchError, fetch_jd_text
from jobagent.jd import MatchError
from jobagent.llm import ChatClient, LLMError
from jobagent.matcher import Matcher, match_job
from jobagent.notify import Notification, Notifier, NotifyError, build_channels
from jobagent.profile import DEFAULT_PROFILE_DIR, load_profile, profile_warnings
from jobagent.resolver import Resolver
from jobagent.storage import BLOCK_REASONS, Storage


def _reporting_load_errors(load):
    try:
        return load()
    except FileNotFoundError as error:
        print(f"ERROR: file not found: {error.filename}")
    except ValidationError as error:
        print(f"ERROR: invalid config\n{error}")
    return None


def _load_settings(args):
    return _reporting_load_errors(lambda: load_settings(args.settings))


def _load_all(args):
    return _reporting_load_errors(
        lambda: (load_settings(args.settings), load_preferences(args.preferences), load_profile(args.profile_dir))
    )


def _check_config(args, env: Mapping[str, str]) -> int:
    loaded = _load_all(args)
    if loaded is None:
        return 1
    settings, prefs, profile = loaded

    print(f"LLM provider: {settings.llm.provider}")
    for name in settings.llm.tasks:
        resolved = resolve_task(settings, name)
        print(f"  {name} -> {resolved.provider} / {resolved.model}")

    warnings = check_config(settings, prefs, env) + profile_warnings(profile)
    if warnings:
        print(f"Warnings ({len(warnings)}):")
        for warning in warnings:
            print(f"  - {warning}")
    else:
        print("No warnings.")
    return 0


def _answer(args, llm, env: Mapping[str, str]) -> int:
    loaded = _load_all(args)
    if loaded is None:
        return 1
    settings, prefs, profile = loaded

    storage = Storage(args.db or db_path(settings))
    resolver = Resolver(
        profile, prefs, settings, llm or ChatClient(settings, env=env), extra_bank=storage.approved_answers()
    )
    try:
        result = resolver.resolve(args.question, max_length=args.max_length, jd_summary=args.jd_summary)
    except LLMError as error:
        print(f"ERROR: {error}")
        return 1

    if result.answer is None:
        item = storage.queue_answer(args.question)
        print(f"BLOCKED: {result.blocked_reason}\nQueued for your input (id {item.id}).")
        return 2
    if result.needs_review:
        item = storage.queue_answer(args.question, answer=result.answer)
        print(
            f"[{result.source}] {result.answer}\n"
            f"Queued for review (id {item.id}): approve it in the review page before it is used."
        )
        return 0
    print(f"[{result.source}] {result.answer}")
    return 0


def _llm_test(args, llm, env: Mapping[str, str]) -> int:
    settings = _load_settings(args)
    if settings is None:
        return 1
    if args.task not in settings.llm.tasks:
        print(f"ERROR: unknown task '{args.task}'; choose from {sorted(settings.llm.tasks)}")
        return 1

    resolved = resolve_task(settings, args.task)
    print(f"{resolved.provider} / {resolved.model}")
    started = time.perf_counter()
    try:
        reply = (llm or ChatClient(settings, env=env)).complete(
            args.task, "You are a connectivity check.", "Reply with the single word OK."
        )
    except LLMError as error:
        print(f"ERROR: {error}")
        return 1
    print(f"reply: {reply.strip()!r}\n{time.perf_counter() - started:.1f}s")
    return 0


def _models(args, llm, env: Mapping[str, str]) -> int:
    settings = _load_settings(args)
    if settings is None:
        return 1
    provider = args.provider or settings.llm.provider
    if provider not in settings.llm.providers:
        print(f"ERROR: unknown provider '{provider}'; choose from {sorted(settings.llm.providers)}")
        return 1
    try:
        names = (llm or ChatClient(settings, env=env)).list_models(provider)
    except LLMError as error:
        print(f"ERROR: {error}")
        return 1
    print("\n".join(names))
    return 0


def _match(args, llm, env: Mapping[str, str]) -> int:
    loaded = _load_all(args)
    if loaded is None:
        return 1
    settings, prefs, profile = loaded

    if args.text_file:
        try:
            jd_text = Path(args.text_file).read_text(encoding="utf-8")
        except OSError as error:
            print(f"ERROR: cannot read {args.text_file}: {error}")
            return 1
    else:
        try:
            jd_text = fetch_jd_text(args.url)
        except FetchError as error:
            print(f"ERROR: {error}")
            return 1

    storage = Storage(args.db or db_path(settings))
    matcher = Matcher(profile, prefs, settings, llm or ChatClient(settings, env=env))
    try:
        outcome = match_job(storage, matcher, args.url, jd_text, force=args.force)
    except (MatchError, LLMError) as error:
        print(f"ERROR: {error}")
        return 1

    if outcome.skipped:
        print(
            f"SKIPPED: already tracked as job #{outcome.job.id} with status '{outcome.job.status}'. "
            "Use --force to evaluate it again."
        )
        return 0
    result = outcome.result
    score = f" (score {result.score:g})" if result.score is not None else ""
    print(f"DECISION: {result.decision}{score}\nreason: {result.reason}")
    if result.summary:
        print(f"summary: {result.summary}")
    print(f"Saved as job #{outcome.job.id}, status {outcome.job.status}.")
    return 0


def _batch(args, llm, env: Mapping[str, str]) -> int:
    loaded = _load_all(args)
    if loaded is None:
        return 1
    settings, prefs, profile = loaded

    try:
        urls, bad = read_urls(args.file)
    except OSError as error:
        print(f"ERROR: cannot read {args.file}: {error}")
        return 1
    for token in bad:
        print(f"ERROR '{token}': not a web address (it must start with http:// or https://)")
    if not urls:
        print(f"No web addresses in {args.file}.")
        return 1 if bad else 0

    storage = Storage(args.db or db_path(settings))
    matcher = Matcher(profile, prefs, settings, llm or ChatClient(settings, env=env))
    result = run_batch(storage, matcher, urls, fetch=fetch_jd_text, force=args.force)
    result.counts["errors"] += len(bad)

    counts = result.counts
    print(
        f"Done: {counts['apply']} apply, {counts['watch_later']} watch_later, {counts['reject']} reject, "
        f"{counts['skipped']} skipped, {counts['errors']} errors."
    )
    return 1 if counts["errors"] or result.aborted else 0


def _calibrate(args, llm, env: Mapping[str, str]) -> int:
    loaded = _load_all(args)
    labels = _reporting_load_errors(lambda: load_labels(args.labels))
    if loaded is None or labels is None:
        return 1
    settings, prefs, profile = loaded
    if not labels.jobs:
        print(f"No labelled jobs in {args.labels}.")
        return 0

    matcher = Matcher(profile, prefs, settings, llm or ChatClient(settings, env=env))
    rows = run_calibration(matcher, labels, Path(args.labels).parent)
    for row in rows:
        score = "-" if row.score is None else f"{row.score:g}"
        if row.error:
            print(f"ERR   {row.file}: {row.error}")
        elif row.agrees:
            print(f"ok    {row.file}: {row.label} (score {score})")
        else:
            print(f"DIFF  {row.file}: you: {row.label}, matcher: {row.decision} (score {score}): {row.reason}")
    agreed = sum(row.agrees for row in rows)
    print(f"Agreement: {agreed}/{len(rows)} ({100 * agreed / len(rows):.0f}%)")
    return 0


def _usable_channels(settings, channels):
    if channels is not None:
        return channels, []
    return build_channels(settings)


def _notify_test(args, channels) -> int:
    settings = _load_settings(args)
    if settings is None:
        return 1
    active, notes = _usable_channels(settings, channels)
    for note in notes:
        print(f"NOTE: {note}")
    if not active:
        print("No notification channels are usable. Check notifications in settings.yaml.")
        return 1
    failures = 0
    for channel in active:
        try:
            channel.send(Notification("jobagent test", "If you can see and hear this, alerts work."))
            print(f"ok    {channel.name}")
        except NotifyError as error:
            failures += 1
            print(f"FAILED {channel.name}: {error}")
    return 1 if failures else 0


def _block(args, channels) -> int:
    settings = _load_settings(args)
    if settings is None:
        return 1
    storage = Storage(args.db or db_path(settings))
    try:
        block = storage.block_job(
            args.job_id, args.reason, detail=args.detail, page_url=args.page_url, screenshot_path=args.screenshot
        )
    except ValueError as error:
        print(f"ERROR: {error}")
        return 1

    active, notes = _usable_channels(settings, channels)
    for note in notes:
        print(f"NOTE: {note}")
    report = Notifier(storage, active, settings.notifications.cooldown_minutes).notify_block(block)

    print(f"Job #{block.job_id} blocked: {block.reason}.")
    if report.suppressed:
        print("Alert suppressed: a similar alert went out recently.")
    elif report.sent:
        print(f"Alert sent via {', '.join(report.sent)}.")
    elif not active:
        print("No notification channels are usable, so nobody was alerted.")
    for name, error in report.failed.items():
        print(f"FAILED {name}: {error}")
    return 0


def _blocks(args) -> int:
    settings = _load_settings(args)
    if settings is None:
        return 1
    storage = Storage(args.db or db_path(settings))
    blocks = storage.list_blocks()
    if not blocks:
        print("No open blocks.")
        return 0
    for block in blocks:
        job = storage.get_job(block.job_id)
        print(f"block #{block.id}  job #{block.job_id}  {block.reason}  {block.detail or '-'}  {job.url}")
    return 0


def _resume(args) -> int:
    settings = _load_settings(args)
    if settings is None:
        return 1
    try:
        job = Storage(args.db or db_path(settings)).resume_job(args.job_id)
    except ValueError as error:
        print(f"ERROR: {error}")
        return 1
    print(f"Job #{job.id} is back in the apply queue (status {job.status}).")
    return 0


def main(
    argv: Optional[Sequence[str]] = None,
    llm=None,
    env: Optional[Mapping[str, str]] = None,
    channels=None,
) -> int:
    parser = argparse.ArgumentParser(prog="jobagent")
    commands = parser.add_subparsers(dest="command", required=True)

    def add_settings_option(command):
        command.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS)

    def add_config_options(command):
        add_settings_option(command)
        command.add_argument("--preferences", type=Path, default=DEFAULT_PREFERENCES)
        command.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)

    add_config_options(commands.add_parser("check-config", help="validate settings, preferences and profile"))

    answer = commands.add_parser("answer", help="resolve one application-form question")
    answer.add_argument("question")
    answer.add_argument("--max-length", type=int, default=None)
    answer.add_argument("--jd-summary", default="")
    answer.add_argument("--db", type=Path, default=None)
    add_config_options(answer)

    llm_test = commands.add_parser("llm-test", help="send one tiny request to a task's model")
    llm_test.add_argument("--task", default="jd_summary")
    add_settings_option(llm_test)

    models = commands.add_parser("models", help="list the model ids a provider offers")
    models.add_argument("--provider", default=None)
    add_settings_option(models)

    match = commands.add_parser("match", help="evaluate one job posting and save it to the tracker")
    match.add_argument("url")
    match.add_argument("--text-file", type=Path, default=None, help="job description saved as text (skips fetching)")
    match.add_argument("--force", action="store_true", help="evaluate again even if already tracked")
    match.add_argument("--db", type=Path, default=None)
    add_config_options(match)

    batch = commands.add_parser("batch", help="evaluate every job address in a text file (one per line)")
    batch.add_argument("file", type=Path)
    batch.add_argument("--force", action="store_true", help="evaluate again even if already tracked")
    batch.add_argument("--db", type=Path, default=None)
    add_config_options(batch)

    calibrate = commands.add_parser("calibrate", help="compare the matcher with your own labels")
    calibrate.add_argument("labels", type=Path)
    add_config_options(calibrate)

    notify_test = commands.add_parser("notify-test", help="send a test alert to every notification channel")
    add_settings_option(notify_test)

    block = commands.add_parser("block", help="mark a job as needing you and send an alert")
    block.add_argument("job_id", type=int)
    block.add_argument("--reason", required=True, choices=BLOCK_REASONS)
    block.add_argument("--detail", default="")
    block.add_argument("--page-url", default=None)
    block.add_argument("--screenshot", default=None, help="path to a screenshot to attach to the alert")
    block.add_argument("--db", type=Path, default=None)
    add_settings_option(block)

    blocks = commands.add_parser("blocks", help="list the jobs waiting for you")
    blocks.add_argument("--db", type=Path, default=None)
    add_settings_option(blocks)

    resume = commands.add_parser("resume", help="put a blocked job back in the apply queue")
    resume.add_argument("job_id", type=int)
    resume.add_argument("--db", type=Path, default=None)
    add_settings_option(resume)

    args = parser.parse_args(argv)
    env = load_env() if env is None else env
    handlers = {
        "notify-test": lambda: _notify_test(args, channels),
        "block": lambda: _block(args, channels),
        "blocks": lambda: _blocks(args),
        "resume": lambda: _resume(args),
        "check-config": lambda: _check_config(args, env),
        "answer": lambda: _answer(args, llm, env),
        "llm-test": lambda: _llm_test(args, llm, env),
        "models": lambda: _models(args, llm, env),
        "match": lambda: _match(args, llm, env),
        "batch": lambda: _batch(args, llm, env),
        "calibrate": lambda: _calibrate(args, llm, env),
    }
    return handlers[args.command]()
