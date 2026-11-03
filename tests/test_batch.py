import pytest
from fakes import FakeLLM
from matching import JD_TEXT, make_llm, make_matcher, scoring

from jobagent.batch import read_urls, run_batch
from jobagent.fetch import FetchError
from jobagent.llm import LLMError
from jobagent.matcher import match_job
from jobagent.storage import Storage

URLS = [f"https://boards.greenhouse.io/acme/jobs/{n}" for n in (1, 2, 3)]


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "jobs.db")


class Fetcher:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    def __call__(self, url):
        self.calls.append(url)
        if url in self.fail_for:
            raise FetchError(f"cannot reach {url}")
        return JD_TEXT


def run(storage, llm, urls=URLS, fetch=None, force=False):
    lines = []
    fetch = fetch or Fetcher()
    result = run_batch(storage, make_matcher(llm), urls, fetch=fetch, force=force, say=lines.append)
    return result, lines, fetch


def test_read_urls_ignores_blank_lines_and_comments_and_reports_lines_that_are_not_web_addresses(tmp_path):
    path = tmp_path / "urls.txt"
    path.write_text(
        "# Greenhouse\nhttps://a.example/1\n\n  https://b.example/2   # frontend, remote\nnot a url\nftp://c.example/3\n",
        encoding="utf-8",
    )

    urls, bad = read_urls(path)

    assert urls == ["https://a.example/1", "https://b.example/2"]
    assert bad == ["not", "ftp://c.example/3"]


def test_every_url_is_fetched_evaluated_and_saved_with_one_line_each(storage):
    result, lines, fetch = run(storage, make_llm())

    assert result.counts == {"apply": 3, "watch_later": 0, "reject": 0, "skipped": 0, "errors": 0}
    assert fetch.calls == URLS
    assert [j.status for j in storage.list_jobs()] == ["queued"] * 3
    assert len(lines) == 3
    assert "[1/3]" in lines[0] and "apply (score 85.7)" in lines[0]
    assert "Frontend Engineer at Acme" in lines[0] and URLS[0] in lines[0]


def test_a_mix_of_outcomes_is_counted(storage):
    result, _, _ = run(storage, make_llm(skill_scoring=scoring(typescript="partial")))

    assert result.counts["watch_later"] == 3 and result.counts["apply"] == 0


def test_links_already_tracked_are_skipped_without_fetching_them(storage):
    match_job(storage, make_matcher(make_llm()), URLS[0], JD_TEXT)

    result, lines, fetch = run(storage, make_llm(), urls=URLS[:2])

    assert fetch.calls == [URLS[1]]
    assert result.counts["skipped"] == 1 and result.counts["apply"] == 1
    assert "SKIPPED" in lines[0] and "queued" in lines[0]


def test_force_evaluates_tracked_links_again(storage):
    match_job(storage, make_matcher(make_llm()), URLS[0], JD_TEXT)

    result, _, fetch = run(storage, make_llm(), urls=URLS[:1], force=True)

    assert fetch.calls == [URLS[0]] and result.counts["apply"] == 1 and result.counts["skipped"] == 0


def test_a_page_that_cannot_be_fetched_does_not_stop_the_others(storage):
    result, lines, fetch = run(storage, make_llm(), fetch=Fetcher(fail_for=[URLS[1]]))

    assert result.counts["errors"] == 1 and result.counts["apply"] == 2
    assert "ERROR" in lines[1] and URLS[1] in lines[1]
    assert storage.get_job_by_url(URLS[1]) is None


def test_an_unreadable_model_reply_is_a_per_job_error_and_the_batch_carries_on(storage):
    llm = make_llm(extraction="nonsense")

    result, lines, fetch = run(storage, llm, urls=URLS[:2])

    assert result.counts["errors"] == 2 and fetch.calls == URLS[:2]
    assert all("ERROR" in line and "no JSON" in line for line in lines)
    assert [j.status for j in storage.list_jobs()] == ["new", "new"]


def test_a_model_that_cannot_be_reached_stops_the_whole_batch(storage):
    llm = FakeLLM(jd_extraction=LLMError("huggingface returned HTTP 402"))

    result, lines, fetch = run(storage, llm)

    assert fetch.calls == URLS[:1]
    assert result.counts["errors"] == 1
    assert result.aborted == "huggingface returned HTTP 402"
    assert "Stopping" in lines[-1]
