from pathlib import Path

import yaml
from fakes import FakeLLM
from matching import JD_TEXT, make_llm

from jobagent.cli import main

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def write_labels(tmp_path, items, write_files=True):
    folder = tmp_path / "cal"
    (folder / "jd").mkdir(parents=True)
    if write_files:
        for name, _ in items:
            (folder / "jd" / name).write_text(JD_TEXT, encoding="utf-8")
    labels = folder / "labels.yaml"
    labels.write_text(
        yaml.safe_dump({"jobs": [{"file": f"jd/{name}", "label": label} for name, label in items]}), encoding="utf-8"
    )
    return labels


def calibrate(labels, llm, capsys):
    code = main(
        [
            "calibrate", str(labels),
            "--settings", str(FIXTURES_DIR / "settings.yaml"),
            "--preferences", str(FIXTURES_DIR / "preferences.yaml"),
            "--profile-dir", str(FIXTURES_DIR / "profile"),
        ],
        llm=llm,
    )
    return code, capsys.readouterr().out


def test_calibrate_reports_agreement_and_every_disagreement(tmp_path, capsys):
    labels = write_labels(tmp_path, [("a.txt", "apply"), ("b.txt", "reject"), ("c.txt", "watch_later")])

    code, out = calibrate(labels, make_llm(), capsys)

    assert code == 0
    assert "Agreement: 1/3 (33%)" in out
    assert out.count("DIFF") == 2
    assert "b.txt" in out and "you: reject, matcher: apply" in out


def test_calibrate_with_full_agreement(tmp_path, capsys):
    labels = write_labels(tmp_path, [("a.txt", "apply"), ("b.txt", "apply")])

    _, out = calibrate(labels, make_llm(), capsys)

    assert "Agreement: 2/2 (100%)" in out and "DIFF" not in out


def test_a_missing_job_file_is_an_error_row_that_counts_against_agreement(tmp_path, capsys):
    labels = write_labels(tmp_path, [("a.txt", "apply")], write_files=False)

    code, out = calibrate(labels, make_llm(), capsys)

    assert code == 0
    assert "ERR" in out and "a.txt" in out and "Agreement: 0/1 (0%)" in out


def test_a_matcher_failure_on_one_job_does_not_stop_the_run(tmp_path, capsys):
    labels = write_labels(tmp_path, [("a.txt", "apply"), ("b.txt", "apply")])

    _, out = calibrate(labels, FakeLLM(jd_extraction="nonsense"), capsys)

    assert out.count("ERR") == 2 and "Agreement: 0/2 (0%)" in out


def test_an_empty_label_file_says_there_is_nothing_to_compare(tmp_path, capsys):
    labels = write_labels(tmp_path, [])

    code, out = calibrate(labels, make_llm(), capsys)

    assert code == 0 and "No labelled jobs" in out


def test_an_unknown_label_is_rejected(tmp_path, capsys):
    labels = write_labels(tmp_path, [("a.txt", "maybe")])

    code, out = calibrate(labels, make_llm(), capsys)

    assert code == 1 and "label" in out


def test_a_missing_label_file_is_reported(tmp_path, capsys):
    code, out = calibrate(tmp_path / "nope.yaml", make_llm(), capsys)

    assert code == 1 and "nope.yaml" in out
