# TASKS

Update this file after every small step. See `AGENTS.md` for the handoff protocol.

## Milestones (from PLAN.md)

- [x] P0 Scaffold: git, AGENTS.md, TASKS.md, package skeleton, test runner
- [x] P1 Config files and loader, `check-config`, SQLite job tracker (`jobs` table) and storage module
- [x] P1b Profile templates (personal, experience, skills, education, answers) with schemas, loader and warnings
- [x] P2 Answer resolver, LLM client, review queue, `answer` command, Streamlit review page
- [x] P3 Matcher: JD extraction, hard filters, skill scoring, decision, `evaluations` table, `match` and `calibrate` commands
- [x] P4 Notifier (ntfy and desktop), `blocks` table, `block` / `blocks` / `resume` / `notify-test` commands
- [x] P5 `batch` command: feed a file of job URLs, match and log (no applying). Code and tests done; the plan's acceptance run (20 real URLs, real model) is NOT done yet

## Next

The foundation plan (P0 to P5) is built. The detailed, ordered list is in `HANDOVER.md` section 9; in short:
1. Get one real model working (the Hugging Face token is set but the account has no Inference Providers credits: HTTP 402; Groq or Ollama are the alternatives), then run `jobagent llm-test`, `jobagent answer`, and `jobagent batch` on about 20 real postings. Expect to tune the prompts (small models may break the JSON replies).
2. Fill `profile/*.yaml` (personal, experience, skills), then label ~25 postings in `calibration/` and run `jobagent calibrate` to tune thresholds and weights.
3. Then the pieces the plan left out of scope: job discovery, the browser applicant (Playwright, uses `block_job` and `Notifier` from P4), email/OTP reading, a tracker dashboard, full prompt-injection hardening.

## Where I stopped

Task 4 completed: Multi-Agent Real-Time UI cards & Expandable Application Journey Timeline in `src/jobagent/dashboard.py`. All 257 tests passing.
Next: Task 5 (End-to-End Verification & Documentation).

## Notes for whoever continues

- Tests use frozen copies in `tests/fixtures/`, never the owner's live files in `config/` and `profile/`, so editing those never breaks tests. `test_real_config_files_are_valid` and `test_real_profile_files_are_valid` only check that the live files parse and validate.
- `tests/fakes.py::FakeLLM` stands in for the model (a reply can be an exception instance). `tests/matching.py` has the shared matcher fixtures (`make_llm`, `make_matcher`, `scoring`).
- The model client (`llm.py`) talks to any OpenAI-compatible endpoint with plain `urllib`. It sends `User-Agent: jobagent/0.1` because Groq's Cloudflare returns 403 (error 1010) for Python's default one. Providers: local, openai, groq, huggingface. Keys come from `.env` (`config.load_env`) or the environment.
- NOT yet done: a real completion against any provider (the `HF_TOKEN` is now set, but the account returns HTTP 402: no Inference Providers credits). `llm-test` and `models` exist for that. The matcher and resolver have only run against `FakeLLM`, so expect to tune the prompts once a real model answers (small local models may break the JSON replies).
- Groq model ids in `settings.yaml` are unverified (listing needs a key). Hugging Face ids were checked against its public list.
- Sensitive-question rules live in `resolver.py` (`SENSITIVE_PATTERNS`). Sensitive topics only ever reuse an exact curated answer, never a fuzzy or model match, and are never drafted. Keep it that way.
- Approved answers are stored in the SQLite `answers` table, not written back to `answers.yaml`.
- Matcher: the model extracts fields and rates each listed skill full/partial/none; code does the weighted maths and the decision (`matcher.py`). Hard filters (`filters.py`) reject only on clear evidence; unknown values pass. A posting is evaluated once per link unless `--force`; applied/blocked/failed jobs are never re-evaluated.
- `fetch.py` is plain HTTP. It worked on Lever, Greenhouse and python.org pages; JS-only pages give no text, and a corporate-proxy certificate error (`CERTIFICATE_VERIFY_FAILED`) appeared on example.com on the owner's network.
- `calibration/labels.yaml` is empty on purpose: the owner saves ~25 JDs under `calibration/jd/` and labels them, then runs `jobagent calibrate`.
- Notifier (`notify.py`): channels are `NtfyChannel` and `DesktopChannel`; `Notifier.notify_block` holds back repeats of the same reason on the same site inside `notifications.cooldown_minutes`. Verified live: ntfy.sh round trip on a throwaway topic (title, Hindi and accented text, click link, high priority, PNG attachment) and one desktop toast plus beep through `notify-test` (ran without error; whether the toast was visible on screen was not confirmed). The owner has since set a real ntfy topic and confirmed a test alert arrived; the placeholder topic is still never used. The topic is effectively a password for a public channel, so move it from `settings.yaml` to `.env` before the repo is pushed anywhere.
- Tests must never build the real `DesktopChannel` through `main`; pass `channels=[...]` (see `tests/test_cli.py`) or a toast will pop up during the test run.
- The applicant does not exist yet. It will call `storage.block_job` and `Notifier.notify_block` itself; `jobagent block` is only the manual way to trigger the same thing.
- `config/settings.yaml` model names are guesses for local/openai; the owner should check them against what is installed.
- YAML pitfall seen once: a list must be either `[a, b]` or one `- item` per line, never `[` followed by `- item` lines.
- Streamlit is an optional extra: `pip install -e ".[dev,ui]"`. The review page reads `JOBAGENT_DB` if set, else the path in settings.
- The owner is moving the project to a MacBook and will use Ollama: see `MACBOOK_SETUP.md`. `DesktopChannel` only works on Windows (it raises `NotifyError` elsewhere), so on a Mac set `notifications.desktop.toast` and `sound` to false until a macOS branch is added (`HANDOVER.md` section 9, item 2). All development and test runs so far were on Windows.
- Nothing is committed yet; the first commit is the owner's call.

## Log

- P0: git init, venv, AGENTS.md, CLAUDE.md, TASKS.md, pyproject, package skeleton.
- P1: `config.py`, `cli.py` (`check-config`), `storage.py` (job tracker). Test first throughout.
- Fixed invalid YAML in `profile/preferences.yaml` (bracket and dash list styles mixed) without changing the owner's values.
- P1b: `profile.py` plus five templates in `profile/`; `check-config` also reports profile warnings. Tests moved onto frozen fixtures.
- P2: `text.py`, `llm.py`, `resolver.py`, `answers` table and queue API, `jobagent answer`, `review_ui.py`. Review page checked in a real browser against a scratch database.
- Providers: groq and huggingface, `.env` loading, `llm-test`, `models`, the User-Agent fix. Only the active (or pinned) provider needs models. Active provider set to huggingface.
- P5: `batch.py` (`read_urls`, `run_batch`), `already_handled` in `matcher.py` so tracked links are not fetched, `jobagent batch`, and `tests/test_end_to_end.py` (real HTTP: fake job pages and a fake OpenAI-compatible model server through the real fetch, `ChatClient`, matcher, tracker and batch).
- P4: `blocks` table and `block_job` / `resume_job` / alert bookkeeping in `storage.py`, `notify.py`, `jobagent notify-test|block|blocks|resume`, `notifications.cooldown_minutes`.
- P3: `jd.py` (lenient extraction model), `filters.py` (nine hard filters), `matcher.py` (scoring, decision, `match_job`), `fetch.py`, `calibrate.py`, `evaluations` table, `jobagent match` and `jobagent calibrate`, `calibration/labels.yaml` template.
- Handover: wrote `HANDOVER.md` (briefing for a new LLM: built, verified, left, gotchas) and `MACBOOK_SETUP.md` (moving to macOS and running with Ollama). Checked the Windows `tar` zip command in the guide: 9.3 MB, no `.venv`, `.env` or `data`.
- MacBook setup (uv): installed dev/ui dependencies with uv, created 8k-context Ollama model qwen2.5-coder-7b-8k:latest, updated settings.yaml (provider: local, desktop notifications: false), verified check-config, models, llm-test (0.4s response), notify-test, and all 236 pytest tests.
- Documentation: created comprehensive usage and post-setup run instructions in README.md.
- Feature: built visible Chrome Browser Applicant (`applicant.py`, `form_inspector.py`) and full-featured Streamlit Control Center (`dashboard.py`). Added 12 new automated tests (248 total passing).
