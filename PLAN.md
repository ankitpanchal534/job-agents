# Job-Apply Agent: Foundation Plan (for review)

Scope: the knowledge layer, job tracker (SQLite), human handoff, matcher and minimal guardrails. Not in scope here: job discovery, the browser applicant, email/OTP reading, dashboard, full security hardening.

## 1. Goals

| # | Goal | Done when |
|---|------|-----------|
| A | Profile and answer bank (your points 1 and 2) | Any form question resolves to a curated answer, a grounded generated answer, or a "needs human" block. Never an invented fact. |
| B | Human-block notifications (point 3) | When the agent is stuck, you get a sound plus a phone push with screenshot and link. The agent keeps working on other jobs. |
| C | Matcher and watch-later (point 4) | Given a job URL, the system returns apply / watch-later / reject with a score and reasons. |
| D | Minimal guardrails (point 5) | Matcher has no tools, applicant is domain-allowlisted. Full hardening deferred. |
| E | Job tracker | Every job link is stored the moment it is found, with its summarised JD, match score and status, and every later stage updates that row. Duplicates are never applied to twice. |

## 2. Proposed layout

```
agentic-ai/
  profile/
    personal.yaml        name, contact, links, address, languages
    experience.yaml      per role: short / medium / long summary, achievements, tech used
    education.yaml
    skills.yaml          skill, years, level, tier (core | secondary)
    preferences.yaml     target roles, locations, salary floor, work auth, notice period,
                         blacklist, apply threshold, hard filters
    answers.yaml         FAQ answer bank (curated)
    resumes/             resume variants (pdf)
    cover_letters.md     templates
  data/jobs.db           SQLite (job tracker, evaluations, answers log, blocks)
  src/                   loader, resolver, matcher, notifier, storage, cli
```

Facts live in YAML (validated by a schema). Narrative text lives in markdown or in long-form YAML fields.

## 3. Workstream A: Profile and answer bank

**Answer resolution order** (first hit wins; built in P2, `src/jobagent/resolver.py`):
1. **Curated bank, exact**: the question matches an entry in `answers.yaml` or an answer you approved earlier, ignoring case and punctuation. Used verbatim.
2. **Curated bank, near-identical**: same, above `resolver.fuzzy_match_cutoff`. Never used for sensitive topics, because "sponsorship in the US" and "in the UK" look almost identical.
3. **Profile field**: name, email, phone, city, country, links, current company and title. Exact label match only, no model involved.
4. **Preference fact**: notice period and relocation, straight from `preferences.yaml`.
5. **Curated bank, model-matched**: for a reworded question, the model picks among the closest curated questions and must be at least `resolver.llm_match_confidence` sure. Not used for sensitive topics.
6. **Block** if the question is sensitive: queued for your input, never drafted.
7. **Grounded draft**: for open questions ("why this company", "describe a project") the model writes an answer from the profile (no contact details) plus the JD summary. Marked `generated` and queued for your review. If the profile lacks the facts, or the draft exceeds the field limit, it is blocked instead.

**Never generated (curated only, else block):** work authorization / visa, salary and compensation, notice period, relocation, criminal or background checks, disability, EEO/demographic questions, references.

**Learning loop:** approved answers are saved in the SQLite `answers` table and join the curated bank on the next lookup, so the model is needed less over time. (Changed from the original idea of writing back into `answers.yaml`: that would rewrite your file and drop its comments. `answers.yaml` stays yours to edit by hand.)

**Review page:** a Streamlit page (`streamlit run src/jobagent/review_ui.py`) lists blocked questions that need your answer and drafts that need approval. You can edit a draft before approving; edited or typed answers are stored as `human`.

**Experience versions:** each role has short (1 line), medium (3-4 lines) and long (full) forms so the agent picks the length a field allows.

**Deliverables:** schemas (Pydantic), loader, resolver, review queue as a local web page (Streamlit), starter templates for each file.

## 4. Workstream B: Human-block notifications

- **Block reasons:** CAPTCHA, login expired, unknown/unsupported question, unsupported portal, submit not confirmed.
- **Channels:** Windows toast plus sound (local) and a phone push (ntfy) carrying a screenshot and the job link. One abstraction, channels pluggable.
- **Behavior:** the job moves to `blocked` with saved page URL and screenshot. The worker continues with other jobs. You resolve it, then run `resume <job_id>` (or reply from the phone later).
- **Noise control:** rate-limit and batch notifications so one bad portal doesn't spam you.

**As built (P4):**
- `blocks` table: job, reason (`captcha`, `login_expired`, `unknown_question`, `unsupported_portal`, `submit_unconfirmed`, `other`), detail, page URL, screenshot path, site, and when it was created, alerted and resolved. Blocking a job sets its status to `blocked`; the same open block is never recorded twice.
- `jobagent block <job_id> --reason ...` records a block and alerts; `jobagent blocks` lists what is waiting; `jobagent resume <job_id>` resolves the block and puts the job back in the apply queue (`queued`). The future applicant will call the same functions instead of the command.
- Channels: ntfy (POST, or PUT with the screenshot as an attachment; non-ASCII text is header-encoded) and desktop (Windows toast through PowerShell plus a beep). The toast text travels in environment variables, never inside the script. One failing channel never stops the others or the block itself.
- Noise control: the same reason on the same site inside `notifications.cooldown_minutes` (default 15) sends one alert; the next one after the window carries "+N similar since the last alert". An alert is only counted as sent when at least one channel delivered it.
- The ntfy placeholder topic is never used: ntfy.sh topics are public, so an unset topic is skipped with a note.
- `jobagent notify-test` sends a test alert to every usable channel.

## 5. Workstream C: Matcher and watch-later

**Pipeline per job:**
1. **Fetch and parse the JD.** The LLM extracts structured data: title, seniority, years required, location/remote, salary if stated, must-have skills, nice-to-have skills.
2. **Hard filters** (deterministic, before any scoring): location/remote, seniority and years range, salary floor, work authorization, company blacklist, already-applied duplicate. Fail means reject with reason.
3. **Skill score:** compare JD requirements to `skills.yaml`. Must-haves weigh more than nice-to-haves. Output score 0-100, missing must-haves, and a one-line reason.
4. **Decision:** score >= `apply_threshold` (default 75, configurable) goes to the apply queue. Score between the watch floor (default 50) and the threshold goes to watch-later. Below the floor is rejected. All decisions are logged.

**Watch-later list:** not a separate store. It is the set of tracker rows with status `watch_later`, holding link, score, missing skills, reason and date. You can export it, promote any entry to apply with one command, and re-evaluate the list when your profile changes.

**As built (P3):**
- The model does two judgments: it extracts the posting's fields (`jd_extraction`), then rates each skill the job lists as `full`, `partial` or `none` against your skills (`skill_scoring`). Code does the arithmetic: partial counts half, must-haves weigh `matcher.must_have_weight` times nice-to-haves, and the score maps to a decision through `apply_threshold` and `watch_floor`. So the score is explainable and tunable from `settings.yaml`.
- Hard filters reject only on clear evidence: a value the posting does not state always passes. Every failed filter is listed in the reason.
- A job that fails a filter skips scoring and summarising, which saves two model calls. A posting with no listed skills goes to `watch_later` for you to look at.
- Decisions map to tracker statuses: `apply` to `queued`, `watch_later`, `reject` to `rejected`. Each run adds a row to `evaluations` (extracted fields, filter reasons, score, missing must-haves, reason, models used).
- A link already evaluated is skipped unless `--force`; a job that is applied, blocked or failed is never re-evaluated.
- Fetching is plain HTTP (http and https only). Pages that build their content with JavaScript yield no text; save the description to a file and use `--text-file`.
- The matcher has no tools. The job text reaches the model only inside `<job_description>` tags and is described to it as untrusted; the scoring step never sees the job text at all, only the skill lists.

**As built (P5):** `jobagent batch urls.txt` reads one job address per line (blank lines and `#` comments ignored, anything after the address is a comment) and runs the same fetch, match and save for each, printing one line per job and a summary. Links already tracked are skipped without being fetched (`--force` evaluates them again). A page that cannot be fetched or a model reply that cannot be read is an error for that job only. A model that cannot be used at all (no key, no credits, unreachable) stops the whole batch, because every remaining job would fail the same way. Jobs run one after another; running several at once is a later speed-up.

**Calibration:** you label about 25 real JDs (apply / watch / skip). We tune the threshold and weights until the matcher agrees with you on most of them, and log disagreements. Save each JD as text under `calibration/jd/`, list it in `calibration/labels.yaml` with your verdict, and run `jobagent calibrate calibration/labels.yaml`.

**Model:** chosen per task from `config/settings.yaml` (see section 9), with structured JSON output.

## 6. Workstream D: Minimal guardrails

- Matcher agent has no tools: text in, JSON out.
- JD text is passed as delimited data, never as instructions.
- Applicant (later) gets a domain allowlist and a fixed action set.
- No secrets or profile data in prompts beyond what the task needs.
- Full prompt-injection hardening is deferred.

## 7. Storage and job tracker (SQLite)

**Decision:** SQLite is the source of truth. It is one local file with no accounts or network, which fits an agent running on this machine. IndexedDB was ruled out because the agent process cannot read browser storage and the data would vanish with the Edge profile. Firebase was deferred because job history would leave the machine and every write would need the network.

**Storage interface:** all reads and writes go through a small `storage` module, so the backend can change without touching the agents.

**Optional later:** if you want a phone or dashboard view, add a one-way sync of tracker rows only (link, status, score) to Firestore. The profile files and answer bank stay local.

**Job tracker lifecycle:**
1. Discovery saves the link as a new row (unique URL, so duplicates are caught here).
2. Fetch and parse adds company, title and the summarised JD.
3. The matcher adds score, reasons, missing skills and the decision.
4. The applicant and submit stages update status, ending in `applied` or `failed`, or `blocked` when you are needed.

**Statuses:** `new`, `rejected`, `watch_later`, `queued`, `blocked`, `applied`, `failed`.

**Tables:**
- `jobs` (the tracker): id, url (unique), source, company, title, jd_summary, status, score, created_at, updated_at
- `evaluations`: job_id, extracted_json, hard_filter_result, score, missing_skills, decision, reason, model, created_at
- `answers` (review queue and learned bank, built in P2): id, job_id, question, answer, source (generated | human), status (pending_review | needs_input | approved | rejected), timestamps
- `blocks`: job_id, reason, screenshot_path, page_url, created_at, resolved_at

## 8. Milestones

| Phase | Work | Acceptance |
|-------|------|------------|
| P0 | Resolve open questions below, scaffold repo | Repo runs |
| P1 | Profile files, schemas, loader, starter templates, config loader with `check-config`, SQLite job tracker plus storage interface | Invalid or missing fields fail loudly; a job link can be saved, updated through every status, and a duplicate URL is rejected |
| P2 | Answer resolver plus review queue (Streamlit page) | Sample question set: no fabricated facts, sensitive questions always blocked |
| P3 | Matcher plus watch-later (tracker status) plus label set | Agrees with your labels on most of ~25 JDs; every decision has a reason and is written to the tracker |
| P4 | Notifier plus block/resume | Forced block produces sound, push with screenshot, and a working resume |
| P5 | CLI: feed it job URLs, it matches and logs (no applying) | End-to-end run on 20 URLs without manual fixes |

## 9. Configuration and LLM routing

Everything you tune lives in two files you edit by hand. The agent will validate them on start, and a planned `python -m jobagent check-config` command (built in P1) will report problems and warnings.

| File | You edit | Contains |
|------|----------|----------|
| `profile/preferences.yaml` | target roles, titles to exclude, seniority, years range, countries/cities, remote preference, salary floor, work authorization, notice period, blacklist | The hard filters and the matcher's idea of a good job |
| `config/settings.yaml` | LLM provider toggle, per-task models, thresholds, ntfy, review page, storage path, domain allowlist | How the system runs |
| `.env` | `OPENAI_API_KEY` | Secrets only; never in the YAML files |

**LLM toggle:** `llm.provider` is one of `local`, `openai`, `groq` or `huggingface`; more can be added in `settings.yaml`. All four speak the OpenAI-compatible API, so one client serves them. Local means Ollama or LM Studio on this machine; Hugging Face goes through its Inference Providers router. Keys live in `.env` (`HF_TOKEN`, `GROQ_API_KEY`, `OPENAI_API_KEY`).

**Per-task models:** each task (JD extraction, JD summary, skill scoring, answer matching, answer generation, vision) names a model for the active provider and for any provider it is pinned to. Other providers' models can be added later; `check-config` warns about the gaps. `jobagent models --provider <name>` lists the real model ids, and `jobagent llm-test` sends one tiny request to check a setup. The global toggle flips all tasks at once. A task can also set its own `provider` to override the toggle, for example vision on OpenAI while everything else stays local.

**Defaults** ship in the files: apply threshold 75, watch floor 50, must-have weight above nice-to-have.

## 10. Decisions

| Question | Decision |
|----------|----------|
| Target roles, locations, seniority, salary | You configure these in `profile/preferences.yaml`; empty values produce a warning |
| Language | Python |
| LLM | Local, OpenAI, Groq and Hugging Face, switched by one config toggle, with models set per task. Hugging Face first for testing |
| Phone push | ntfy |
| Answer review | Local web page, built with Streamlit for speed |
| Thresholds | Defaults in `config/settings.yaml`, adjustable there |
| Remote view | Desktop only for now, so the optional Firestore sync is deferred |
