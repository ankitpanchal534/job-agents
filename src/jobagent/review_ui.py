import os

import streamlit as st

from jobagent.config import db_path, load_settings
from jobagent.storage import Answer, Storage


def _storage() -> Storage:
    override = os.environ.get("JOBAGENT_DB")
    return Storage(override) if override else Storage(db_path(load_settings()))


def _card(storage: Storage, item: Answer) -> None:
    st.markdown(f"**{item.question}**")
    text = st.text_area("Answer", value=item.answer or "", key=f"text_{item.id}")
    approve, reject = st.columns(2)
    if approve.button("Approve", key=f"approve_{item.id}", type="primary"):
        if not text.strip():
            st.error("Type an answer first.")
        else:
            storage.approve_answer(item.id, final_answer=text.strip())
            st.rerun()
    if reject.button("Reject", key=f"reject_{item.id}"):
        storage.reject_answer(item.id)
        st.rerun()


def main() -> None:
    storage = _storage()
    st.title("Answer review")

    needs_input = storage.list_answers(status="needs_input")
    drafts = storage.list_answers(status="pending_review")

    if not needs_input and not drafts:
        st.markdown("Nothing is waiting for review.")
    if needs_input:
        st.header(f"Needs your answer ({len(needs_input)})")
        st.caption("Sensitive or unknown questions. The agent never guesses these.")
        for item in needs_input:
            _card(storage, item)
    if drafts:
        st.header(f"Drafts to review ({len(drafts)})")
        st.caption("Written by the model from your profile. Edit if needed, then approve.")
        for item in drafts:
            _card(storage, item)

    st.caption(f"{len(storage.approved_answers())} approved answers are in the bank.")


main()
