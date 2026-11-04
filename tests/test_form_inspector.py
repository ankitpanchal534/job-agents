from __future__ import annotations

import pytest
from jobagent.form_inspector import FormField, normalize_field_prompt, find_best_select_option


def test_form_field_creation():
    field = FormField(
        name="full_name",
        tag="input",
        input_type="text",
        label="Full Name",
        placeholder="Jane Doe",
        is_required=True,
        selector="input[name='full_name']",
    )
    assert field.label == "Full Name"
    assert field.is_required is True
    assert field.tag == "input"


def test_normalize_field_prompt_prioritizes_label():
    field = FormField(
        name="applicant_email_address",
        tag="input",
        input_type="email",
        label="Email Address *",
        placeholder="you@domain.com",
    )
    prompt = normalize_field_prompt(field)
    assert prompt == "Email Address"


def test_normalize_field_prompt_falls_back_to_placeholder_or_name():
    field_placeholder = FormField(
        name="q123",
        tag="input",
        input_type="text",
        label="",
        placeholder="LinkedIn Profile URL",
    )
    assert normalize_field_prompt(field_placeholder) == "LinkedIn Profile URL"

    field_name = FormField(
        name="phone_number",
        tag="input",
        input_type="tel",
        label="",
        placeholder="",
    )
    assert normalize_field_prompt(field_name) == "phone number"


def test_find_best_select_option():
    options = ["Please choose...", "Yes", "No", "Decline to state"]
    # If answer is 'Yes', should match 'Yes'
    assert find_best_select_option(options, "Yes") == "Yes"
    # Case-insensitive / substring match
    assert find_best_select_option(options, "yes") == "Yes"
    # No match returns None
    assert find_best_select_option(options, "Maybe") is None

