## 1. Tools

Install Homebrew from <https://brew.sh> if you do not have it, then:

```bash
brew install python@3.12
python3 --version
```

Any Python 3.10 or newer works. If `python3 --version` shows something older, use `python3.12` in the commands below.

## 2. Set up the project

```bash
cd ~/Projects          # any folder you like
unzip ~/Downloads/jobagent-transfer.zip
cd agentic-ai
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,ui]"
.venv/bin/python -m pytest -q
```

You should see `236 passed`. If something fails on the Mac but passed on Windows, that is a portability bug worth fixing: send the failure output to your LLM.

If you prefer, `source .venv/bin/activate` once per terminal, and then write `python` instead of `.venv/bin/python` in every command below.

## 3. Install Ollama and a model

Either install the app from <https://ollama.com/download> (it starts the server for you), or use Homebrew:

```bash
brew install ollama
brew services start ollama
```

Pull a model. Pick by how much memory your Mac has (sizes are rough):

| Mac memory | Model to use | Download size | Notes |
|---|---|---|---|
| 8 GB | `qwen2.5:3b` | about 2 GB | Fast, but weaker; it may break the JSON replies the matcher needs |
| 16 GB | `qwen2.5:7b` | about 5 GB | The default in `settings.yaml`; a good starting point |
| 32 GB or more | `qwen2.5:14b` | about 9 GB | Better answers, used for drafting in `settings.yaml` |

```bash
ollama pull qwen2.5:7b
ollama list
```

Check the server answers:

```bash
curl http://localhost:11434/api/tags
```

### Give the model a bigger context window

Ollama's default context window is small (often 4096 tokens), and a job description can be longer than that. Text beyond the window is silently cut, which hurts the matcher. Make a copy of the model with a larger window:

```bash
printf 'FROM qwen2.5:7b\nPARAMETER num_ctx 8192\n' > Modelfile
ollama create qwen2.5-7b-8k -f Modelfile
ollama list
```

Then use `qwen2.5-7b-8k` as the model name in `settings.yaml` (next step). Newer Ollama versions may also let you set a default window with the `OLLAMA_CONTEXT_LENGTH` environment variable; the Modelfile way works everywhere.

## 4. Point jobagent at Ollama

Edit `config/settings.yaml`:

1. Set the provider: `provider: local` under `llm:`.
2. Make the `local:` model names match what `ollama list` shows exactly. Each task has a line like `models: { local: "qwen2.5:7b", ... }`. Change `"qwen2.5:7b"` to `"qwen2.5-7b-8k"` (or whatever you pulled). `answer_generation` uses `qwen2.5:14b` and `vision` uses `qwen2.5vl:7b`; if you did not pull those, set them to a model you have.
3. Turn off the desktop alerts (they are Windows-only for now). Under `notifications:` set:

```yaml
  desktop:
    toast: false
    sound: false
```

ntfy keeps working, and you can read alerts in the ntfy app on your Mac as well as your phone.

## 5. Check that it works

```bash
.venv/bin/python -m jobagent check-config
.venv/bin/python -m jobagent models
.venv/bin/python -m jobagent llm-test
```

- `check-config` lists what is still empty (profile files, allowed domains). Warnings are normal until you fill the profile.
- `models` should list your Ollama models.
- `llm-test` should print `local / <your model>` and `reply: 'OK'`. The first call is slow because the model is loading into memory.

Send a test alert to your phone:

```bash
.venv/bin/python -m jobagent notify-test
```

## 6. Use it

Fill in `profile/personal.yaml`, `experience.yaml` and `skills.yaml` first. Without skills the matcher has nothing to compare, and without experience the answer drafts are blocked.

```bash
.venv/bin/python -m jobagent answer "Why do you want to work here?"
.venv/bin/python -m jobagent match "https://example.com/job" --text-file jd.txt
.venv/bin/python -m jobagent batch urls.txt
.venv/bin/python -m jobagent calibrate calibration/labels.yaml
.venv/bin/python -m streamlit run src/jobagent/review_ui.py
```

- `match` takes a job link. If the page builds its content with JavaScript you get "no readable text": save the job description into a file and pass `--text-file`.
- `batch` takes a text file with one job link per line (`#` starts a comment).
- The review page opens in your browser at <http://localhost:8501>.
- To label jobs for `calibrate`, save each description as text in `calibration/jd/` and list it in `calibration/labels.yaml` (the file explains the format).

## 7. If something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| `cannot reach http://localhost:11434/v1` | Ollama is not running. Open the app, or `brew services start ollama`, or run `ollama serve` in another terminal |
| HTTP 404 or "model not found" | The model name in `settings.yaml` is not the one `ollama list` shows. Fix the name or `ollama pull` it |
| Very slow or the Mac freezes | The model is too big for your memory. Use a smaller one (see the table in step 3) |
| "contained no JSON" or "not usable" errors | The model answered in the wrong format. Try a larger model, or the 8192-token copy from step 3 |
| `FAILED desktop: ... only work on Windows` | Step 4, point 3: switch the desktop alerts off in `settings.yaml` |
| `CERTIFICATE_VERIFY_FAILED` when fetching a page | If you installed Python from python.org, run its "Install Certificates.command"; with Homebrew Python, `brew reinstall python@3.12`. On a work network a proxy may also be the cause |
| `ModuleNotFoundError: streamlit` | Reinstall with the extras: `.venv/bin/python -m pip install -e ".[dev,ui]"` |
| Port 8501 already in use | Add `--server.port 8502` to the `streamlit run` command |

## 8. Still to build (see `HANDOVER.md`, section 9)

The job finder, the form-filling applicant and the email/OTP reader do not exist yet. What you have is the part that reads job postings, scores them against your profile, answers form questions safely, tracks everything in a local database and alerts you.
