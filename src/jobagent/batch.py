from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from jobagent.fetch import FetchError, fetch_jd_text
from jobagent.jd import MatchError
from jobagent.llm import LLMError
from jobagent.matcher import Matcher, already_handled, match_job
from jobagent.storage import Storage


@dataclass
class BatchResult:
    counts: dict[str, int]
    aborted: Optional[str] = None


def read_urls(path: Path) -> tuple[list[str], list[str]]:
    urls, bad = [], []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token = line.split()[0]
        parts = urlsplit(token)
        if parts.scheme.lower() in ("http", "https") and parts.netloc:
            urls.append(token)
        else:
            bad.append(token)
    return urls, bad


def run_batch(
    storage: Storage,
    matcher: Matcher,
    urls: list[str],
    fetch: Callable[[str], str] = fetch_jd_text,
    force: bool = False,
    say: Callable[[str], None] = print,
) -> BatchResult:
    result = BatchResult(counts={"apply": 0, "watch_later": 0, "reject": 0, "skipped": 0, "errors": 0})
    for number, url in enumerate(urls, 1):
        tag = f"[{number}/{len(urls)}]"

        handled = already_handled(storage, url, force)
        if handled is not None:
            say(f"{tag} SKIPPED job #{handled.id} is already {handled.status}: {url}")
            result.counts["skipped"] += 1
            continue

        try:
            text = fetch(url)
            outcome = match_job(storage, matcher, url, text, force=force)
        except (FetchError, MatchError) as error:
            say(f"{tag} ERROR {url}: {error}")
            result.counts["errors"] += 1
            continue
        except LLMError as error:
            say(f"{tag} ERROR {url}: {error}")
            say("Stopping: the model could not be used, so the remaining jobs would fail the same way.")
            result.counts["errors"] += 1
            result.aborted = str(error)
            break

        match = outcome.result
        score = f" (score {match.score:g})" if match.score is not None else ""
        say(
            f"{tag} {match.decision}{score}  {match.posting.title or '(untitled)'} "
            f"at {match.posting.company or 'unknown company'}  {url}"
        )
        result.counts[match.decision] += 1
    return result
