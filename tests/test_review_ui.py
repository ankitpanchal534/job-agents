from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from jobagent.storage import Storage

APP = Path(__file__).resolve().parents[1] / "src" / "jobagent" / "review_ui.py"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    monkeypatch.setenv("JOBAGENT_DB", str(db))
    return Storage(db)


def run_app():
    app = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not app.exception
    return app


def page_text(app):
    return " ".join(element.value for element in app.markdown)


def test_empty_queue_says_nothing_is_waiting(storage):
    assert "Nothing is waiting" in page_text(run_app())


def test_drafts_and_blocked_questions_are_listed(storage):
    storage.queue_answer("Why this company?", answer="A draft.")
    storage.queue_answer("What are your salary expectations?")

    text = page_text(run_app())

    assert "Why this company?" in text
    assert "What are your salary expectations?" in text


def test_approving_a_draft_adds_it_to_the_bank_unchanged(storage):
    draft = storage.queue_answer("Why this company?", answer="A draft.")

    app = run_app()
    app.button(key=f"approve_{draft.id}").click().run()

    assert not app.exception
    assert storage.approved_answers() == [("Why this company?", "A draft.")]
    assert storage.list_answers(status="approved")[0].source == "generated"


def test_an_edited_draft_is_saved_as_a_human_answer(storage):
    draft = storage.queue_answer("Why this company?", answer="A draft.")

    app = run_app()
    app.text_area(key=f"text_{draft.id}").set_value("My own words.").run()
    app.button(key=f"approve_{draft.id}").click().run()

    approved = storage.list_answers(status="approved")[0]
    assert (approved.answer, approved.source) == ("My own words.", "human")


def test_a_blocked_question_cannot_be_approved_without_typing_an_answer(storage):
    item = storage.queue_answer("What are your salary expectations?")

    app = run_app()
    app.button(key=f"approve_{item.id}").click().run()

    assert any("Type an answer" in error.value for error in app.error)
    assert storage.list_answers(status="needs_input")[0].id == item.id


def test_a_typed_answer_to_a_blocked_question_joins_the_bank(storage):
    item = storage.queue_answer("What are your salary expectations?")

    app = run_app()
    app.text_area(key=f"text_{item.id}").set_value("12 LPA, negotiable.").run()
    app.button(key=f"approve_{item.id}").click().run()

    assert storage.approved_answers() == [("What are your salary expectations?", "12 LPA, negotiable.")]


def test_rejecting_removes_the_item_without_adding_it_to_the_bank(storage):
    draft = storage.queue_answer("Why this company?", answer="A draft.")

    app = run_app()
    app.button(key=f"reject_{draft.id}").click().run()

    assert storage.approved_answers() == []
    assert storage.list_answers(status="rejected")[0].id == draft.id
