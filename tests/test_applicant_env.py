from __future__ import annotations

import os
from pathlib import Path


def test_playwright_importable():
    import playwright
    from playwright.sync_api import sync_playwright

    assert playwright is not None
    assert sync_playwright is not None


def test_chrome_executable_found_on_macos():
    # Common Chrome executable locations on macOS
    candidates = [
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        Path(os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
    ]
    found = any(path.exists() for path in candidates)
    assert found, f"Google Chrome executable not found in candidate paths: {candidates}"

