from __future__ import annotations

import pytest
from jobagent.visual_inspector import VisualField, normalize_visual_label, scan_visual_form
from playwright.sync_api import sync_playwright


def test_visual_field_creation():
    field = VisualField(
        field_type="text",
        label="Full Name",
        center_x=200.0,
        center_y=350.0,
        width=300.0,
        height=40.0,
        is_required=True,
    )
    assert field.label == "Full Name"
    assert field.center_x == 200.0
    assert field.center_y == 350.0
    assert field.is_required is True


def test_normalize_visual_label():
    assert normalize_visual_label("Full Name ✱") == "Full Name"
    assert normalize_visual_label("Location (City) *") == "Location (City)"
    assert normalize_visual_label("Phone Number •") == "Phone Number"
    assert normalize_visual_label("Current Company:\n") == "Current Company"


def test_scan_visual_form_extracts_coordinates():
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
      <style>
        .control { display: block; width: 300px; height: 35px; margin-bottom: 20px; }
      </style>
    </head>
    <body style="margin: 0; padding: 20px;">
      <label for="first_name">First Name ✱</label>
      <input id="first_name" name="first_name" type="text" class="control" required>

      <label for="country">Country</label>
      <select id="country" name="country" class="control">
        <option value="">Select country...</option>
        <option value="India">India</option>
        <option value="USA">United States</option>
      </select>

      <button type="submit" id="submit_btn" style="width: 150px; height: 40px;">Submit</button>
    </body>
    </html>
    """
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content(html_content)

        fields = scan_visual_form(page)
        browser.close()

    assert len(fields) >= 3
    labels = [f.label for f in fields]
    assert any("First Name" in l for l in labels)
    assert any("Country" in l for l in labels)
    assert any("Submit" in l for l in labels)

    fn = next(f for f in fields if "First Name" in f.label)
    assert fn.center_x > 0
    assert fn.center_y > 0
    assert fn.is_required is True
    assert fn.field_type == "text"

    c = next(f for f in fields if "Country" in f.label)
    assert c.field_type == "dropdown"
    assert "India" in c.options
