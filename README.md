# JobAgent

`jobagent` is a local, privacy-first multi-agent assistant that:
1. **Reads and evaluates job postings** (single URLs or batch lists).
2. **Scores jobs against your profile** using deterministic hard filters + LLM skill match weighting.
3. **Resolves application-form questions safely** using a curated bank and profile-grounded drafts without inventing personal facts.
4. **Maintains a local SQLite job tracker** (`data/jobs.db`) for all decisions, evaluations, and answer logs.
5. **Supports human-in-the-loop handoff** with alerts (ntfy push notifications) and an interactive web review dashboard.

---

## 1. Quick Start / Environment

Commands can be run directly using **`uv`** (recommended) or inside an activated virtual environment:

```bash
# Using uv:
uv run python -m jobagent <command>

# Or with an activated environment:
source .venv/bin/activate
python -m jobagent <command>
```

> **Note:** If using a local LLM via Ollama, ensure the Ollama service is running (`open -a Ollama` or `ollama list`) before executing LLM-dependent commands.

---

## 2. Profile Setup (Before Running)

The system compares postings and drafts answers using the facts in `profile/`. Edit these files with your real details:

| File | Purpose |
|---|---|
| [`profile/personal.yaml`](profile/personal.yaml) | Name, email, phone number, location, and social/portfolio links. |
| [`profile/skills.yaml`](profile/skills.yaml) | Core and secondary skills, years of experience, and proficiency levels. |
| [`profile/experience.yaml`](profile/experience.yaml) | Past roles with short, medium, and long summaries (used for answer drafting). |
| [`profile/education.yaml`](profile/education.yaml) | Degrees, institutions, and graduation dates. |
| [`profile/preferences.yaml`](profile/preferences.yaml) | Desired roles, locations, salary floors, notice period, and blacklist. |
| [`profile/answers.yaml`](profile/answers.yaml) | Curated answers to recurring application questions. |

### Validate Configuration
Run the configuration checker at any time to verify your YAML files and check for missing fields:

```bash
uv run python -m jobagent check-config
```

---

## 3. How to Use / Core Commands

### A. Evaluate a Single Job Posting (`match`)
Extracts job requirements, evaluates hard filters (location, seniority, work authorization), scores matching skills, and saves the verdict (`apply` / `watch_later` / `reject`) to the local SQLite database:

```bash
uv run python -m jobagent match "https://example.com/job-posting-url"
```

If the job posting is protected by Cloudflare, logins, or heavy JavaScript rendering, save the description text to a file and pass `--text-file`:

```bash
uv run python -m jobagent match "https://example.com/job-posting-url" --text-file jd.txt
```

To re-evaluate a link that is already in the database:
```bash
uv run python -m jobagent match "https://example.com/job-posting-url" --force
```

---

### B. Batch Evaluate Job Postings (`batch`)
Evaluate a list of job URLs sequentially. Create a text file (e.g. `urls.txt`) containing one URL per line (supports `#` comments):

```text
# urls.txt
https://boards.greenhouse.io/example/jobs/12345
https://jobs.lever.co/example/67890
```

Run the batch evaluation:
```bash
uv run python -m jobagent batch urls.txt
```

---

### C. Resolve Application Form Questions (`answer`)
Resolves an application question using the hierarchy: exact curated match → near-identical match → profile facts → grounded LLM draft. Sensitive topics (salary, visas, demographics) are never guessed and will prompt for human review:

```bash
uv run python -m jobagent answer "Why do you want to work here?"
```

Optional parameters:
- `--max-length <chars>`: Enforces a maximum character limit.
- `--jd-summary "<summary>"`: Provides context from the specific job posting.

---

### D. Full Control Dashboard (`Streamlit Web UI`)
Launch the comprehensive web control center to run everything through an intuitive UI:
- Paste single or batch URLs and trigger live runs.
- Watch Google Chrome open on your laptop and fill forms visibly in real-time.
- View streaming terminal logs and execution progress steps.
- Browse the full Applications Tracker table (status, timestamps, match scores, links).
- Inspect incidents & stuck jobs (view form screenshots, resolve CAPTCHAs, click "Resume Job").
- Review and approve drafted answers or provide missing profile answers.
- Run system diagnostics (`check-config`, `llm-test`, `notify-test`).

```bash
uv run streamlit run src/jobagent/dashboard.py
```
Open **[http://localhost:8501](http://localhost:8501)** in your browser.

---

### E. Visible Browser Applicant & Agent Hands (`apply`)
Run the browser applicant with virtual mouse & keyboard hands (`BrowserHands`) to open your installed Google Chrome and watch it fill out the job application:

```bash
# Dry-run mode (fills form, pauses before clicking submit, waits on block)
uv run python -m jobagent apply "https://example.com/job-url" --dry-run

# Full submission mode
uv run python -m jobagent apply "https://example.com/job-url"

# Run without waiting for human on blocks (exits immediately if blocked)
uv run python -m jobagent apply "https://example.com/job-url" --no-wait

# Attach a specific resume PDF
uv run python -m jobagent apply "https://example.com/job-url" --resume path/to/custom_resume.pdf
```

Key features of the Browser Applicant:
- **Virtual Mouse & Keyboard ("Agent Hands")**: Injects an active custom neon cursor (`#jobagent-cursor`) into Chrome, moves smoothly along natural quadratic Bézier trajectories, displays live action badges ("Typing...", "Clicking...", "Focusing..."), produces visual click ripples, and types with human micro-delays.
- **Complete Journey Audit**: Saves every step of the job application journey into SQLite (`journey_steps` table): what was extracted, match score, matched/missing skills, exact fields filled, resume uploaded, and any incident screenshots.
- **In-Place Block Waiting & Resume**: If an application encounters a CAPTCHA or sensitive question, the browser window stays open and waits for you to solve it in Chrome or click "Resume" in the dashboard. Once solved, it immediately resumes without reloading or closing!

---

### F. Human Handoff & Notification Commands

When an automated process is stuck (e.g., CAPTCHA, unconfirmed submit, unknown question), it enters a `blocked` state and can alert you:

- **Send a test notification alert**:
  ```bash
  uv run python -m jobagent notify-test
  ```
- **List jobs waiting for your review**:
  ```bash
  uv run python -m jobagent blocks
  ```
- **Resume a blocked job back into the apply queue**:
  ```bash
  uv run python -m jobagent resume <job_id>
  ```
- **Manually flag a job as blocked**:
  ```bash
  uv run python -m jobagent block <job_id> --reason captcha
  ```

---

### F. Calibrate Matcher Accuracy (`calibrate`)
Tune matcher thresholds and scoring weights against your own manual verdicts:

1. Save sample job descriptions under `calibration/jd/`.
2. Map your verdicts (`apply`, `watch_later`, `reject`) in `calibration/labels.yaml`.
3. Run the calibration:
   ```bash
   uv run python -m jobagent calibrate calibration/labels.yaml
   ```

---

## 4. Diagnostics & Testing

- **Verify LLM connectivity**:
  ```bash
  uv run python -m jobagent llm-test
  ```
- **List provider models**:
  ```bash
  uv run python -m jobagent models
  ```
- **Run test suite**:
  ```bash
  uv run pytest
  ```

---

## 5. Storage

Data is tracked locally in SQLite at `data/jobs.db`:
- `jobs`: Tracked job postings, titles, URLs, and current application status (`found`, `queued`, `applied`, `watch_later`, `rejected`, `blocked`, `failed`).
- `evaluations`: Extraction outputs, filter match details, skill scores, and decisions.
- `answers`: Approved and queued question-and-answer pairs.
- `blocks`: Human-block events, reasons, screenshot references, and resolution status.
- `journey_steps`: Chronological audit trail of all actions, inputs, scores, and decisions across all agents.
