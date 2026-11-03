# jobagent: handover for a new LLM or developer

Written 2026-11-04. This file is the one-stop briefing. The detail lives in `PLAN.md` (design and decisions), `TASKS.md` (status and notes) and `AGENTS.md` (working rules). Read this first, then those three.

## 1. Paste this to the new LLM

> You are taking over an existing Python project called `jobagent`, in this folder. Before you change anything:
> 1. Read `HANDOVER.md`, `AGENTS.md`, `TASKS.md` and `PLAN.md`.
> 2. Run `git status` and the test suite (`.venv/bin/python -m pytest -q`; on Windows `.venv\Scripts\python -m pytest -q`).
> 3. Tell me in five lines what you understood and which item from section 9 of `HANDOVER.md` you want to do first, and why. Wait for my answer.
>
> Follow `AGENTS.md`: test first (TDD), verify before saying "done", do not commit unless I ask, never invent my personal facts, secrets only in `.env`. I write in Hinglish (Hindi and English mixed); reply in the language I use, briefly.

## 2. What this project is

The owner wants a multi-agent system that applies to jobs on their behalf:

1. **Discovery agent:** finds job postings and collects links. (NOT built)
2. **Matcher agent:** loads a posting, reads the job description, decides match or not. (BUILT)
3. **Applicant agent:** fills and submits the application on any portal. (NOT built)
4. **Email/OTP tool:** reads a dedicated job-search mailbox to confirm verification links and OTPs. (NOT built)

Agreed shape, after discussion: semi-autonomous first. The agent does everything up to the final Submit and the owner approves; the approval gate is removed only for portals that have proven reliable. "Any portal" was narrowed to start with standard ATS pages (Greenhouse, Lever, Ashby), which have open application forms and public job APIs. LinkedIn and Indeed are avoided for auto-apply because automating them breaks their terms and risks the owner's account. CAPTCHAs are never bypassed: the job is blocked and the owner is alerted.

The owner's personal facts and targets are in `profile/*.yaml` (they edit these; the code never invents values).

## 3. How the owner likes to work

- Communicates in Hinglish. Wants short, direct answers and a clear next step.
- Plans before building (they reviewed `PLAN.md` before any code), then builds one milestone at a time, test first.
- Does not want commits made unless they ask. Nothing is committed yet.
- Wants the project tool-independent: work moves between Claude, GPT and Copilot in VS Code, so state lives in the repo (`TASKS.md`), not in chat.
- Does not want secrets pasted in chat. Keys go in `.env`.
- Asked for recommendations from several vendors, not only Anthropic.
- Skills installed in `.claude/skills` and `.agents/skills`: Superpowers (a chosen subset), `grill-me`/`grilling`, `frontend-design`, `archify`, `caveman` (opt-in only; leave it off).

## 4. Architecture

```
job URL --fetch--> JD text --jd_extraction (LLM)--> JobPosting --hard filters--> reject (reason)
                                                         | pass
        skill_scoring (LLM sees skill lists only, never the job text) + weights --> score
        jd_summary (LLM) -------------------------------------------------------------^
        score >= apply_threshold -> apply | >= watch_floor -> watch_later | else reject
        result -> jobs (status queued / watch_later / rejected) + evaluations row

(future) applicant: queued job -> open form -> Resolver.resolve(question) -> fill -> screenshot
        -> owner approves -> submit -> confirm it landed -> status applied
        stuck (CAPTCHA, login, unknown question) -> storage.block_job + Notifier -> owner -> `resume`
```

| Module (`src/jobagent/`) | Job |
|---|---|
| `config.py` | Settings and preferences models, `load_env` (.env), `resolve_task` (per-task model routing), `check_config` warnings, `db_path` |
| `profile.py` | Schemas, loader and warnings for `profile/*.yaml` |
| `storage.py` | SQLite: `jobs`, `answers`, `evaluations`, `blocks` tables and all queries |
| `llm.py` | `ChatClient`: any OpenAI-compatible `/chat/completions` endpoint with plain `urllib`; `list_models` |
| `resolver.py` | Answers application-form questions; contains the sensitive-question rules |
| `jd.py` | `JobPosting` (lenient parsing of model output) and `extract_posting` |
| `filters.py` | Hard filters (blacklist, title, seniority, years, job type, remote and location, salary floor, work authorization) |
| `matcher.py` | Skill scoring, decision, `match_job`, `already_handled` |
| `fetch.py` | HTTP fetch (http and https only) and HTML to text |
| `batch.py` | URL file reader and `run_batch` |
| `calibrate.py` | Compares the matcher with the owner's own labels |
| `notify.py` | ntfy and desktop channels, `Notifier` with a repeat-alert cooldown |
| `cli.py` | Every command; `review_ui.py` is the Streamlit page for queued answers |

Models are chosen per task in `config/settings.yaml` (`jd_extraction`, `jd_summary`, `skill_scoring`, `answer_matching`, `answer_generation`, `vision`). One global toggle `llm.provider` (`local`, `openai`, `groq`, `huggingface`) switches all tasks; a task can pin its own provider.

## 5. Data model (SQLite, `data/jobs.db`, git-ignored)

- `jobs`: id, url (unique, normalised), source, company, title, jd_summary, status, score, timestamps. Statuses: `new`, `rejected`, `watch_later`, `queued`, `blocked`, `applied`, `failed`. Decision to status: `apply` to `queued`, `watch_later` to `watch_later`, `reject` to `rejected`.
- `evaluations`: one row per evaluation (extracted fields, filter reasons, score, missing must-haves, decision, reason, models used).
- `answers`: review queue and learned answer bank (`pending_review`, `needs_input`, `approved`, `rejected`).
- `blocks`: jobs waiting for the owner (reason, detail, page URL, screenshot path, alert bookkeeping).

## 6. Config files (the owner edits these)

- `config/settings.yaml`: LLM provider and per-task models, thresholds (`matcher.apply_threshold` 75, `watch_floor` 50), resolver cutoffs, ntfy (server, topic, `cooldown_minutes`), desktop alerts, review page, DB path, `guardrails.allowed_domains`.
- `profile/preferences.yaml`: target roles, seniority, countries and cities, remote preference, salary floor, work authorization per country, notice period, blacklist.
- `profile/personal.yaml`, `experience.yaml`, `education.yaml`, `skills.yaml`, `answers.yaml`: the owner's facts. `answers.yaml` is the curated FAQ bank.
- `.env`: `HF_TOKEN`, `GROQ_API_KEY`, `OPENAI_API_KEY` (only the active provider needs one). Never in YAML.
- `calibration/labels.yaml`: the owner's verdicts on saved job descriptions (`calibration/jd/*.txt`).

## 7. Commands

On macOS and Linux use `.venv/bin/python` instead of `.venv\Scripts\python`.

```
python -m jobagent check-config                 validate settings, preferences, profile; lists what is still empty
python -m jobagent llm-test                     one tiny request to the active provider's model
python -m jobagent models --provider <name>     list the model ids a provider offers
python -m jobagent answer "<question>"          resolve one form question; drafts and blocks go to the review queue
python -m streamlit run src/jobagent/review_ui.py   review page for queued answers
python -m jobagent match <url> [--text-file f]  evaluate one posting and save it
python -m jobagent batch urls.txt [--force]     evaluate every link in a file (one per line, # comments)
python -m jobagent calibrate calibration/labels.yaml
python -m jobagent notify-test                  send a test alert to ntfy and the desktop
python -m jobagent block <job_id> --reason captcha | blocks | resume <job_id>
python -m pytest -q                             236 tests
```

## 8. What is built, and what is verified

Built: P0 to P5 of `PLAN.md` (scaffold, config, profile, tracker, answer resolver and review page, matcher with hard filters and weighted scoring, calibration, notifier with blocks and resume, batch). About 2,400 lines of source and 236 tests, written test first.

Verified for real (not only with fakes):
- ntfy.sh round trip (title, Hindi and accented text, click link, high priority, PNG attachment); the owner received a test alert.
- Hugging Face router reachable and its model list read; Groq answers 401 with the fixed `User-Agent`.
- Fetching real public pages (Lever, Greenhouse, python.org).
- The Streamlit review page in a real browser.
- A whole batch over real local HTTP with fake pages and a fake OpenAI-compatible model server (`tests/test_end_to_end.py`).

NOT verified:
- **Any real model completion.** The Hugging Face token is set but the account returns HTTP 402 (no Inference Providers credits). The matcher and resolver have only run against a fake model, so the prompts are untuned and small models may break the JSON replies.
- Groq model ids in `settings.yaml` (listing needs a key) and the local Ollama model ids.
- Everything on macOS or Linux: development and all test runs were on Windows.
- The plan's acceptance run: 20 real postings through `batch`, and agreement with about 25 owner labels through `calibrate`.

## 9. What is left (suggested order)

1. **Get one real model working and tune.** Local Ollama is the plan on the MacBook (`MACBOOK_SETUP.md`). Then run `llm-test`, `answer`, `match` and `batch` on real postings. Likely improvements: send `response_format: {"type": "json_object"}` where the provider supports it, retry once on unreadable JSON, and raise Ollama's context window (job text can reach about 20,000 characters).
2. **macOS desktop alerts.** `DesktopChannel` only works on Windows (PowerShell toast and `winsound`) and raises `NotifyError` elsewhere, so on a Mac `notify-test` and `block` report `FAILED desktop`. Add a macOS branch (`osascript -e 'display notification ...'`, `afplay`), test first with the injected runner. Until then set `notifications.desktop.toast` and `sound` to `false` on the Mac; ntfy still works.
3. **Owner data (not code):** fill `profile/*.yaml`; save about 25 job descriptions in `calibration/jd/`, label them, run `calibrate`, tune thresholds and weights.
4. **Discovery agent.** Pull postings from ATS public JSON APIs, no browser and no model needed (Greenhouse board API, Lever postings API, Ashby job-board API; confirm each endpoint before relying on it). A `sources:` list of company boards in config; add links with `storage.add_job(url, source=...)` (de-duplicated); the job text can come from the API instead of fetching the page; output feeds `batch`.
5. **Applicant agent (largest piece).** Playwright with a dedicated browser profile the owner logs into once (Chrome or Edge, `launch_persistent_context`). First slice: Greenhouse, which needs no login. Steps: open the link (domain must be in `guardrails.allowed_domains`), answer each field through `Resolver.resolve(question, max_length)`, upload the resume, screenshot before submit, hold for the owner's approval, submit, confirm it landed (confirmation page or email) before setting `applied`. When stuck (CAPTCHA, expired login, unknown question, unconfirmed submit) call `storage.block_job` and `Notifier.notify_block`; `resume` re-queues. It must never see raw job-description text as instructions, never answer sensitive questions from a model, never bypass a CAPTCHA.
6. **Email/OTP reader.** IMAP or Gmail API on the dedicated job-search mailbox: find the newest verification link or OTP from an expected sender within a time window. Used only by the applicant.
7. **Tracker dashboard.** A Streamlit page over `jobs` and `evaluations` (filter by status, see score and reason, promote `watch_later` to `queued`).
8. **Speed.** `run_batch` is sequential; model and fetch calls are I/O bound, so a small worker pool is the next step (SQLite opens a connection per call already).
9. **Hardening.** Strip CSS-hidden text when converting HTML, enforce the applicant's domain allowlist in code, review prompts against injection.
10. **Housekeeping.** First git commit; a README; move the ntfy topic from `settings.yaml` to `.env` before pushing the repo anywhere (the topic is effectively a password for a public channel); verify Groq and Ollama model ids.

## 10. Decisions and gotchas not to undo

- **Sensitive questions** (visa and work authorization, salary, notice period, relocation, background, disability, demographics, references) are classified in `resolver.py` (`SENSITIVE_PATTERNS`). They only ever reuse an exact curated answer, never a fuzzy or model match, and are never drafted by a model. Notice period and relocation come straight from `preferences.yaml`. If no answer exists the question is queued for the owner.
- **The matcher has no tools.** Job text is untrusted data: it reaches the model only inside `<job_description>` tags, and the scoring step never receives it at all, only skill lists.
- **Hard filters reject only on clear evidence.** A value the posting does not state passes.
- **A posting is evaluated once per link** unless `--force`; jobs that are applied, blocked or failed are never re-evaluated.
- **Approved answers live in the SQLite `answers` table**, not written back into `answers.yaml` (that would rewrite the owner's file and drop its comments).
- **`llm.py` sends `User-Agent: jobagent/0.1`.** Groq's Cloudflare returns 403 (error 1010) for Python's default one.
- **ntfy.sh topics are public.** The placeholder topic (`CHANGE-ME...`) is never used. The owner has set a real topic.
- **Tests** use frozen copies in `tests/fixtures/`, never the owner's live files. Never build the real `DesktopChannel` through `main` in a test; pass `channels=[...]` or a toast pops up. `tests/fakes.py` (FakeLLM, FakeChannel, Clock) and `tests/matching.py` hold shared helpers.
- **YAML lists** must be `[a, b]` or one `- item` per line, never `[` followed by `- item` lines (this broke the owner's file once).
- `fetch.py` is plain HTTP: JavaScript-only pages give no text (use `--text-file`), and a corporate proxy once caused `CERTIFICATE_VERIFY_FAILED`.
- A provider only needs models for the active (or pinned) provider; other gaps are `check-config` warnings.
- `.archify/` holds the planned-flow diagram (`job-apply-flow.html`, drawn after P1 planning; it predates P2 to P5 details).
