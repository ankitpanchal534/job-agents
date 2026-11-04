from __future__ import annotations

import pytest
from jobagent.browser_hands import BrowserHands, generate_trajectory


def test_browser_hands_initialization():
    hands = BrowserHands(enabled=True)
    assert hands.enabled is True
    assert hands.current_pos == (0, 0)

    hands_disabled = BrowserHands(enabled=False)
    assert hands_disabled.enabled is False


def test_generate_trajectory():
    start = (10.0, 20.0)
    target = (100.0, 200.0)
    steps = 10
    path = generate_trajectory(start, target, steps=steps)

    assert len(path) == steps
    # Path should start close to start and end at target
    assert path[-1] == target
    assert path[0] != start  # first step has moved
    for x, y in path:
        assert isinstance(x, (int, float))
        assert isinstance(y, (int, float))


def test_browser_hands_headless_execution():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, channel="chrome")
        page = browser.new_page()
        page.set_content("""
        <html>
        <body>
            <input id="test-input" type="text" />
            <button id="test-btn" onclick="document.getElementById('test-input').value = 'clicked'">Click me</button>
        </body>
        </html>
        """)

        hands = BrowserHands(enabled=True)
        hands.inject_cursor(page)

        # Check cursor was injected
        cursor = page.locator("#jobagent-cursor")
        assert cursor.count() == 1

        # Move and click button
        btn = page.locator("#test-btn")
        hands.click_element(page, btn)

        input_el = page.locator("#test-input")
        assert input_el.input_value() == "clicked"

        # Type into input
        input_el.fill("")  # reset
        hands.type_into_element(page, input_el, "Hello World")
        assert input_el.input_value() == "Hello World"

        browser.close()
