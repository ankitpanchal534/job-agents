from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class FormField:
    name: str
    tag: str  # 'input', 'textarea', 'select'
    input_type: str  # 'text', 'email', 'tel', 'file', 'select', 'textarea', 'radio', 'checkbox', etc.
    label: str = ""
    placeholder: str = ""
    is_required: bool = False
    selector: str = ""
    options: List[str] = field(default_factory=list)


def normalize_field_prompt(field: FormField) -> str:
    """Derive a clean question/label from a FormField for passing to Resolver."""
    candidate = field.label.strip() if field.label else ""
    if not candidate:
        candidate = field.placeholder.strip()
    if not candidate:
        candidate = field.name.replace("_", " ").replace("-", " ").strip()

    # Strip required asterisks, trailing punctuation, and extra whitespace
    cleaned = re.sub(r"[\*:\s]+$", "", candidate).strip()
    return cleaned


def find_best_select_option(options: Sequence[str], desired: str) -> Optional[str]:
    """Find the best matching option from a dropdown/radio list for a desired answer."""
    desired_clean = desired.strip().lower()
    if not desired_clean:
        return None

    # 1. Exact match (case-insensitive)
    for opt in options:
        if opt.strip().lower() == desired_clean:
            return opt

    # 2. Substring or prefix match
    for opt in options:
        opt_clean = opt.strip().lower()
        if opt_clean and (desired_clean in opt_clean or opt_clean in desired_clean):
            return opt

    return None


def extract_form_fields(page) -> List[FormField]:
    """Inspect the page DOM and return visible, actionable FormField objects."""
    fields: List[FormField] = []
    seen_radio_groups: set[str] = set()

    # Locate all interactive form controls
    elements = page.locator("input:not([type='hidden']):not([type='submit']):not([type='button']), textarea, select").all()

    for idx, el in enumerate(elements):
        try:
            tag_name = el.evaluate("e => e.tagName.toLowerCase()")
            input_type = (el.get_attribute("type") or ("select" if tag_name == "select" else "text")).lower()
            if not el.is_visible() and input_type != "file":
                continue
            name = el.get_attribute("name") or el.get_attribute("id") or f"field_{idx}"
            placeholder = el.get_attribute("placeholder") or ""
            is_required = bool(
                el.get_attribute("required") is not None
                or el.get_attribute("aria-required") == "true"
                or el.evaluate("e => e.required === true")
            )
            elem_id = el.get_attribute("id")

            # -------------------------------------------------------------
            # Special Handling for Radio Button Groups
            # -------------------------------------------------------------
            if input_type == "radio":
                if name in seen_radio_groups:
                    continue
                seen_radio_groups.add(name)

                # Extract all options in this radio group
                group_elements = page.locator(f"input[type='radio'][name='{name}']").all()
                radio_options: List[str] = []
                for r in group_elements:
                    val = r.get_attribute("value") or ""
                    lbl_el = r.locator("xpath=ancestor::label[1]")
                    lbl_text = (lbl_el.inner_text() if lbl_el.count() > 0 else "").strip()
                    chosen = val if val else lbl_text
                    if chosen and chosen not in radio_options:
                        radio_options.append(chosen)

                # Extract question label from ancestor container
                question_label = el.evaluate("""e => {
                    let cur = e;
                    while (cur && cur.parentElement) {
                        cur = cur.parentElement;
                        if (cur.classList.contains("application-question") || cur.classList.contains("custom-question") || cur.tagName === 'FIELDSET') {
                            let lbl = cur.querySelector(".text, .application-label, legend, h3, h4, label");
                            if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                        }
                    }
                    return "";
                }""").strip()

                if not question_label:
                    question_label = name.replace("_", " ").replace("-", " ")

                fields.append(
                    FormField(
                        name=name,
                        tag=tag_name,
                        input_type="radio",
                        label=question_label,
                        is_required=is_required,
                        selector=f"input[type='radio'][name='{name}']",
                        options=radio_options,
                    )
                )
                continue

            # -------------------------------------------------------------
            # Label Extraction for Other Controls (Text, Textarea, Select, File)
            # -------------------------------------------------------------
            label_text = ""
            # 1. aria-label or aria-labelledby
            aria_label = el.get_attribute("aria-label")
            if aria_label:
                label_text = aria_label

            # 2. Check ancestor question container (e.g. Lever, Greenhouse)
            if not label_text or label_text.lower() in ("type your response", "text"):
                container_label = el.evaluate("""e => {
                    let cur = e.closest(".application-question, .custom-question, .form-group, fieldset");
                    if (cur) {
                        let lbl = cur.querySelector(".text, .application-label, legend, label");
                        if (lbl && lbl.innerText.trim()) return lbl.innerText.trim();
                    }
                    return "";
                }""").strip()
                if container_label:
                    label_text = container_label

            # 3. <label for="...">
            if not label_text and elem_id:
                labels = page.locator(f"label[for='{elem_id}']").all()
                if labels:
                    label_text = labels[0].inner_text()

            # 4. Wrapping <label>
            if not label_text:
                parent_label = el.locator("xpath=ancestor::label[1]")
                if parent_label.count() > 0:
                    label_text = parent_label.first.inner_text()

            # 5. Preceding sibling or nearby text
            if not label_text:
                label_text = el.evaluate("""e => {
                    let prev = e.previousElementSibling;
                    if (prev && (prev.tagName === 'LABEL' || prev.tagName === 'SPAN' || prev.tagName === 'DIV')) {
                        return prev.innerText.trim();
                    }
                    return '';
                }""")

            # For select, extract options
            options: List[str] = []
            if tag_name == "select":
                opt_elements = el.locator("option").all()
                options = [opt.inner_text().strip() for opt in opt_elements if opt.inner_text().strip()]

            # Build selector
            if elem_id:
                selector = f"#{elem_id}"
            elif el.get_attribute("name"):
                selector = f"{tag_name}[name='{el.get_attribute('name')}']"
            else:
                selector = f"{tag_name}:nth-of-type({idx+1})"

            fields.append(
                FormField(
                    name=name,
                    tag=tag_name,
                    input_type=input_type,
                    label=label_text.strip(),
                    placeholder=placeholder.strip(),
                    is_required=is_required,
                    selector=selector,
                    options=options,
                )
            )
        except Exception:
            continue

    return fields
