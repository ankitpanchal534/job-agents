from __future__ import annotations

import random
import sys
import time
from typing import Optional, Any


def generate_trajectory(
    start: tuple[float, float],
    target: tuple[float, float],
    steps: int = 15,
) -> list[tuple[float, float]]:
    """Generates a smooth quadratic bezier trajectory between start and target."""
    if steps <= 1:
        return [target]

    x1, y1 = start
    x2, y2 = target

    # Add a slight perpendicular deviation for human-like curvature
    dx = x2 - x1
    dy = y2 - y1
    mid_x = (x1 + x2) / 2
    mid_y = (y1 + y2) / 2

    # 10% perpendicular offset
    ctrl_x = mid_x - dy * 0.1
    ctrl_y = mid_y + dx * 0.1

    points = []
    for step in range(1, steps + 1):
        t = step / steps
        # Quadratic Bézier: B(t) = (1-t)^2 * P0 + 2*(1-t)*t * P1 + t^2 * P2
        inv_t = 1.0 - t
        px = (inv_t ** 2) * x1 + 2 * inv_t * t * ctrl_x + (t ** 2) * x2
        py = (inv_t ** 2) * y1 + 2 * inv_t * t * ctrl_y + (t ** 2) * y2
        points.append((round(px, 1), round(py, 1)))

    # Ensure last point matches target exactly
    points[-1] = (round(x2, 1), round(y2, 1))
    return points


_CURSOR_INJECTION_SCRIPT = """
(() => {
    if (document.getElementById('jobagent-cursor')) return;

    const style = document.createElement('style');
    style.id = 'jobagent-cursor-style';
    style.innerHTML = `
        #jobagent-cursor {
            position: fixed;
            top: 0;
            left: 0;
            pointer-events: none;
            z-index: 2147483647;
            transform: translate(-100px, -100px);
            transition: transform 0.04s ease-out;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        #jobagent-cursor svg {
            filter: drop-shadow(0 2px 6px rgba(0, 0, 0, 0.4));
        }
        #jobagent-cursor-label {
            background: rgba(15, 23, 42, 0.9);
            color: #38bdf8;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 11px;
            font-weight: 600;
            padding: 3px 9px;
            border-radius: 9999px;
            border: 1px solid rgba(56, 189, 248, 0.4);
            box-shadow: 0 2px 8px rgba(0,0,0,0.4);
            white-space: nowrap;
            display: none;
        }
        .jobagent-click-ripple {
            position: fixed;
            top: 0;
            left: 0;
            width: 22px;
            height: 22px;
            margin-left: -11px;
            margin-top: -11px;
            border-radius: 50%;
            background: rgba(37, 99, 235, 0.4);
            border: 2px solid #38bdf8;
            pointer-events: none;
            z-index: 2147483646;
            animation: jobagent-ripple-anim 0.45s cubic-bezier(0.1, 0.8, 0.3, 1) forwards;
        }
        @keyframes jobagent-ripple-anim {
            0% { transform: scale(0.3); opacity: 1; }
            100% { transform: scale(2.8); opacity: 0; }
        }
    `;
    document.head.appendChild(style);

    const cursor = document.createElement('div');
    cursor.id = 'jobagent-cursor';
    cursor.innerHTML = `
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M5.5 3.2L18.5 12.2L12 13.5L9 19.5L5.5 3.2Z" fill="#2563eb" stroke="#ffffff" stroke-width="1.5" stroke-linejoin="round"/>
            <circle cx="12" cy="13.5" r="2.5" fill="#38bdf8"/>
        </svg>
        <span id="jobagent-cursor-label"></span>
    `;
    document.body.appendChild(cursor);
})();
"""


class BrowserHands:
    """Provides visible, human-like virtual mouse movements and keyboard typing via Playwright."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._current_x: float = 0.0
        self._current_y: float = 0.0

    @property
    def current_pos(self) -> tuple[float, float]:
        return (self._current_x, self._current_y)

    def inject_cursor(self, page: Any) -> None:
        """Injects custom cursor DOM element and styles into the active page."""
        if not self.enabled:
            return
        try:
            page.evaluate(_CURSOR_INJECTION_SCRIPT)
        except Exception:
            pass

    def set_label(self, page: Any, text: str) -> None:
        """Sets or hides the action label next to the virtual cursor."""
        if not self.enabled:
            return
        try:
            page.evaluate(
                """([label]) => {
                    const el = document.getElementById('jobagent-cursor-label');
                    if (el) {
                        if (label) {
                            el.textContent = label;
                            el.style.display = 'inline-block';
                        } else {
                            el.style.display = 'none';
                        }
                    }
                }""",
                [text],
            )
        except Exception:
            pass

    def move_to(self, page: Any, target_x: float, target_y: float, steps: int = 12) -> None:
        """Smoothly moves virtual cursor to target coordinates along a curved trajectory."""
        if not self.enabled:
            self._current_x = target_x
            self._current_y = target_y
            return

        self.inject_cursor(page)
        trajectory = generate_trajectory((self._current_x, self._current_y), (target_x, target_y), steps=steps)

        for px, py in trajectory:
            try:
                page.mouse.move(px, py)
                page.evaluate(
                    """([x, y]) => {
                        const el = document.getElementById('jobagent-cursor');
                        if (el) {
                            el.style.transform = `translate(${x}px, ${y}px)`;
                        }
                    }""",
                    [px, py],
                )
            except Exception:
                pass
            time.sleep(0.008)

        self._current_x = target_x
        self._current_y = target_y

    def click_element(self, page: Any, locator: Any, label: str = "Clicking...") -> None:
        """Smoothly moves cursor to element center, shows ripple animation, and clicks."""
        if not self.enabled:
            try:
                locator.click(timeout=3000)
            except Exception:
                locator.click(timeout=3000, force=True)
            return

        self.inject_cursor(page)
        try:
            locator.scroll_into_view_if_needed(timeout=3000)
            box = locator.bounding_box(timeout=3000)
        except Exception:
            box = None

        if box:
            target_x = box["x"] + box["width"] / 2
            target_y = box["y"] + box["height"] / 2

            self.set_label(page, label)
            self.move_to(page, target_x, target_y)

            # Trigger visual ripple
            try:
                page.evaluate(
                    """([x, y]) => {
                        const r = document.createElement('div');
                        r.className = 'jobagent-click-ripple';
                        r.style.transform = `translate(${x}px, ${y}px)`;
                        document.body.appendChild(r);
                        setTimeout(() => r.remove(), 500);
                    }""",
                    [target_x, target_y],
                )
            except Exception:
                pass

            try:
                page.mouse.click(target_x, target_y)
            except Exception:
                locator.click(timeout=3000, force=True)

            time.sleep(0.06)
            self.set_label(page, "")
        else:
            locator.click(timeout=3000, force=True)

    def type_into_element(
        self,
        page: Any,
        locator: Any,
        text: str,
        label: str = "Typing...",
        clear_first: bool = True,
    ) -> None:
        """Focuses element, shows typing indicator, and types with human-like delays."""
        if not self.enabled:
            locator.fill(text)
            return

        self.inject_cursor(page)
        # Focus element with click
        self.click_element(page, locator, label="Focusing...")

        if clear_first:
            try:
                locator.fill("", timeout=3000)
            except Exception:
                pass

        self.set_label(page, label)
        for char in text:
            try:
                page.keyboard.type(char)
            except Exception:
                break
            delay = random.uniform(0.02, 0.05)
            if char in (" ", ",", ".", "@", "-", "_"):
                delay += 0.03
            time.sleep(delay)

        self.set_label(page, "")

    def click_coords(self, page: Any, x: float, y: float, label: str = "Clicking...") -> None:
        """Smoothly moves cursor to physical coordinates (x, y), shows ripple, and clicks."""
        if not self.enabled:
            try:
                page.mouse.click(x, y)
            except Exception:
                pass
            return

        self.inject_cursor(page)
        self.set_label(page, label)
        self.move_to(page, x, y)

        # Trigger visual ripple
        try:
            page.evaluate(
                """([x, y]) => {
                    const r = document.createElement('div');
                    r.className = 'jobagent-click-ripple';
                    r.style.transform = `translate(${x}px, ${y}px)`;
                    document.body.appendChild(r);
                    setTimeout(() => r.remove(), 500);
                }""",
                [x, y],
            )
        except Exception:
            pass

        try:
            page.mouse.click(x, y)
        except Exception:
            pass

        time.sleep(0.06)
        self.set_label(page, "")

    def type_text(
        self,
        page: Any,
        text: str,
        label: str = "Typing...",
        delay_range: tuple[float, float] = (0.02, 0.05),
    ) -> None:
        """Types text via physical keyboard events with realistic human timing."""
        if not self.enabled:
            page.keyboard.type(text)
            return

        self.inject_cursor(page)
        self.set_label(page, label)
        for char in text:
            try:
                page.keyboard.type(char)
            except Exception:
                break
            delay = random.uniform(delay_range[0], delay_range[1])
            if char in (" ", ",", ".", "@", "-", "_"):
                delay += 0.03
            time.sleep(delay)

        self.set_label(page, "")

    def clear_and_type(
        self,
        page: Any,
        x: float,
        y: float,
        text: str,
        label: str = "Typing...",
    ) -> None:
        """Moves to coordinates (x, y), focuses with physical click, selects all, clears, and types."""
        self.click_coords(page, x, y, label="Focusing...")
        time.sleep(0.05)

        # Select all and delete: Meta+A on macOS, Control+A on Windows/Linux
        mod_key = "Meta" if sys.platform == "darwin" else "Control"
        try:
            page.keyboard.down(mod_key)
            page.keyboard.press("KeyA")
            page.keyboard.up(mod_key)
            page.keyboard.press("Backspace")
        except Exception:
            pass

        time.sleep(0.04)
        self.type_text(page, text, label=label)

    def scroll_wheel(
        self,
        page: Any,
        delta_y: float = 500,
        steps: int = 8,
        pause: float = 0.02,
    ) -> None:
        """Performs smooth human-like mouse wheel scrolling."""
        if steps <= 1:
            page.mouse.wheel(0, delta_y)
            return

        step_y = delta_y / steps
        for _ in range(steps):
            try:
                page.mouse.wheel(0, step_y)
            except Exception:
                break
            time.sleep(pause)

