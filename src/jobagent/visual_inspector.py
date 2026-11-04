from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class VisualField:
    field_type: str  # "text", "textarea", "dropdown", "radio", "checkbox", "file", "button"
    label: str
    center_x: float
    center_y: float
    width: float
    height: float
    is_required: bool = False
    options: list[str] = field(default_factory=list)
    name: str = ""
    selector: str = ""
    is_searchable: bool = False
    value: str = ""


_VISUAL_SCAN_SCRIPT = """
() => {
    const fields = [];
    const seenElements = new Set();

    function isVisible(el) {
        if (!el) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width < 4 || rect.height < 4) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        if (el.getAttribute('aria-hidden') === 'true' && el.getAttribute('tabindex') === '-1') return false;
        // Check if element is in the DOM tree
        return el.offsetParent !== null || style.position === 'fixed';
    }

    function cleanText(txt) {
        if (!txt) return '';
        return txt.replace(/[\\n\\r\\t]+/g, ' ').replace(/\\s+/g, ' ').trim();
    }

    function findVisualLabel(el) {
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();

        // 0. For buttons, direct text or value is the visual label
        if (tag === 'button' || type === 'submit' || type === 'button' || el.getAttribute('role') === 'button') {
            const btnText = cleanText(el.innerText || el.value || el.getAttribute('aria-label') || '');
            if (btnText) return btnText;
        }

        // 1. aria-label
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel && cleanText(ariaLabel).length > 1) return cleanText(ariaLabel);

        // 2. aria-labelledby
        const labelledBy = el.getAttribute('aria-labelledby');
        if (labelledBy) {
            const lblEl = document.getElementById(labelledBy);
            if (lblEl && cleanText(lblEl.innerText)) return cleanText(lblEl.innerText);
        }

        // 3. label[for="id"]
        if (el.id) {
            try {
                const labelFor = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
                if (labelFor && cleanText(labelFor.innerText)) return cleanText(labelFor.innerText);
            } catch (e) {}
        }

        // 4. Enclosing label
        const parentLabel = el.closest('label');
        if (parentLabel && cleanText(parentLabel.innerText)) {
            // Remove text of the input itself if any
            return cleanText(parentLabel.innerText);
        }

        // 5. Visual proximity: look at sibling or preceding element above or to the left
        const rect = el.getBoundingClientRect();
        const parent = el.parentElement;
        if (parent) {
            // Check previous sibling
            const prev = el.previousElementSibling;
            if (prev && cleanText(prev.innerText) && cleanText(prev.innerText).length < 150) {
                const pRect = prev.getBoundingClientRect();
                if (pRect.bottom <= rect.top + 20 && pRect.bottom >= rect.top - 80) {
                    return cleanText(prev.innerText);
                }
            }
            // Check parent's preceding sibling or header
            const parentPrev = parent.previousElementSibling;
            if (parentPrev && cleanText(parentPrev.innerText) && cleanText(parentPrev.innerText).length < 150) {
                const ppRect = parentPrev.getBoundingClientRect();
                if (ppRect.bottom <= rect.top + 20 && ppRect.bottom >= rect.top - 100) {
                    return cleanText(parentPrev.innerText);
                }
            }
            // Check enclosing field container header/label
            const fieldWrapper = el.closest('.field, .form-group, .input-group, div[class*="field"], div[class*="form-item"]');
            if (fieldWrapper) {
                const heading = fieldWrapper.querySelector('label, .label, legend, div[class*="label"], span[class*="label"]');
                if (heading && heading !== el && cleanText(heading.innerText)) {
                    return cleanText(heading.innerText);
                }
            }
        }

        // 6. Placeholder
        const placeholder = el.getAttribute('placeholder');
        if (placeholder && cleanText(placeholder).length > 1) return cleanText(placeholder);

        return '';
    }

    // A. Query interactive input controls
    const candidates = Array.from(document.querySelectorAll(
        'input, textarea, select, div[role="combobox"], div.select__control, div[class*="select__control"], button, [role="button"]'
    ));

    for (const el of candidates) {
        if (!isVisible(el)) continue;

        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute('type') || '').toLowerCase();

        // Skip hidden, submit buttons handled separately
        if (type === 'hidden') continue;

        // Skip internal cursor or overlay elements
        if (el.id && el.id.startsWith('jobagent-')) continue;

        const rect = el.getBoundingClientRect();
        const center_x = Math.round(rect.left + rect.width / 2);
        const center_y = Math.round(rect.top + rect.height / 2);

        let field_type = 'text';
        let is_searchable = false;
        let options = [];

        if (tag === 'textarea') {
            field_type = 'textarea';
        } else if (tag === 'select') {
            field_type = 'dropdown';
            options = Array.from(el.options).map(o => (o.text || o.value || '').trim()).filter(Boolean);
        } else if (el.getAttribute('role') === 'combobox' || el.classList.contains('select__control') || (el.className && el.className.includes && el.className.includes('select__control'))) {
            field_type = 'dropdown';
            is_searchable = true;
        } else if (tag === 'button' || type === 'submit' || type === 'button' || el.getAttribute('role') === 'button') {
            field_type = 'button';
        } else if (type === 'radio') {
            field_type = 'radio';
        } else if (type === 'checkbox') {
            field_type = 'checkbox';
        } else if (type === 'file') {
            field_type = 'file';
        } else {
            field_type = 'text';
        }

        const rawLabel = findVisualLabel(el) || el.getAttribute('name') || el.id || '';
        const is_required = el.required ||
            el.getAttribute('aria-required') === 'true' ||
            rawLabel.includes('*') ||
            rawLabel.includes('✱') ||
            rawLabel.includes('•');

        // Build stable CSS selector for DOM options query if needed
        let selector = '';
        if (el.id) {
            selector = `#${CSS.escape(el.id)}`;
        } else if (el.getAttribute('name')) {
            selector = `${tag}[name="${CSS.escape(el.getAttribute('name'))}"]`;
        }

        // Avoid adding duplicated container wrappers when children were already captured
        const key = `${center_x}_${center_y}`;
        if (seenElements.has(key)) continue;
        seenElements.add(key);

        fields.push({
            field_type: field_type,
            label: rawLabel,
            center_x: center_x,
            center_y: center_y,
            width: Math.round(rect.width),
            height: Math.round(rect.height),
            is_required: is_required,
            options: options,
            name: el.getAttribute('name') || '',
            selector: selector,
            is_searchable: is_searchable,
            value: el.value || (el.innerText || '').slice(0, 50)
        });
    }

    // Sort top-to-bottom then left-to-right
    fields.sort((a, b) => {
        if (Math.abs(a.center_y - b.center_y) > 15) {
            return a.center_y - b.center_y;
        }
        return a.center_x - b.center_x;
    });

    return fields;
}
"""


def scan_visual_form(page: Any) -> list[VisualField]:
    """Scans the active browser viewport and returns visible fields with visual coordinates."""
    try:
        raw_fields = page.evaluate(_VISUAL_SCAN_SCRIPT)
    except Exception:
        raw_fields = []

    results: list[VisualField] = []
    for f in raw_fields:
        label = normalize_visual_label(f.get("label", ""))
        results.append(
            VisualField(
                field_type=f.get("field_type", "text"),
                label=label,
                center_x=float(f.get("center_x", 0)),
                center_y=float(f.get("center_y", 0)),
                width=float(f.get("width", 0)),
                height=float(f.get("height", 0)),
                is_required=bool(f.get("is_required", False)),
                options=list(f.get("options", [])),
                name=f.get("name", ""),
                selector=f.get("selector", ""),
                is_searchable=bool(f.get("is_searchable", False)),
                value=f.get("value", ""),
            )
        )
    return results


def normalize_visual_label(label: str) -> str:
    """Strips asterisks, symbols, and whitespace from visual labels."""
    if not label:
        return ""
    # Remove unicode asterisks, stars, bullets
    cleaned = re.sub(r"[✱★•\*\:]+", " ", label)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned
