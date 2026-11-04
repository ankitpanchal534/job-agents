from __future__ import annotations

import pytest
from jobagent.form_inspector import FormField, normalize_field_prompt, find_best_select_option, extract_form_fields
from playwright.sync_api import sync_playwright


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

    # Lever style with unicode heavy asterisk and newline
    field_lever = FormField(
        name="cards[field0]",
        tag="input",
        input_type="text",
        label="Current Compensation (Yearly)\n✱",
    )
    assert normalize_field_prompt(field_lever) == "Current Compensation (Yearly)"


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


def test_extract_form_fields_filters_ghost_and_hidden_elements():
    html = """
    <!DOCTYPE html>
    <html>
    <body>
      <form>
        <label for='fname'>First Name</label>
        <input id='fname' type='text' required>

        <!-- React-Select with dummy validation input -->
        <div id='country-label'>Country*</div>
        <input id='country' type='text' aria-labelledby='country-label' role='combobox'>
        <input required tabindex='-1' aria-hidden='true' class='dummy-react-select'>

        <!-- Hidden dummy element without any label/id/name -->
        <input type='text' style='display:none'>

        <!-- Normal phone -->
        <input id='phone' type='tel' aria-label='Phone Number'>
      </form>
    </body>
    </html>
    """
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome", headless=True)
        page = b.new_page()
        page.set_content(html)
        fields = extract_form_fields(page)
        assert len(fields) == 3
        assert normalize_field_prompt(fields[0]) == "First Name"
        assert normalize_field_prompt(fields[1]) == "Country"
        assert normalize_field_prompt(fields[2]) == "Phone Number"
        b.close()

