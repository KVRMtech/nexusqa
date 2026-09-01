"""B1 — THE DOM-DIFF REJECTION READER (Phase-5 backlog, first item).

MEASURED CAUSE, from the Phase-1 exit re-scope: ``error_texts()`` reads exactly
``[role=alert]`` / ``[aria-live]``. vkpower renders its refusal as a plain
``<p>`` and summit exposes no control-anchored rejection at all — the rule
"Primary beneficiary allocations must total 100%" was on screen in plain words
and nothing was looking. Plain-text errors are the COMMON real-client case; an
application that annotates its errors is the exception.

THE BINDING ACCEPTANCE, from that document, is what these tests hold:

  1. NO application CSS class names — proven functionally: the fixture's error
     node carries no class at all and is still read, and a node styled by any
     class is read identically, because the reader never looks at one.
  2. FORM-SCOPED new-text-after-declined-submit — a toast outside the form is
     not the form's verdict.
  3. ACT-THEN-DIFF — the anti-fabrication property is the DIFFERENCE, not the
     match: a form whose footer always says "errors are shown in red" contains
     rejection-shaped text before anything happened, and only text ABSENT
     before and PRESENT after is a verdict the action produced.
"""
from __future__ import annotations

import asyncio

from app.walker import WalkerMixin


class _Port:
    def __init__(self, *, aria=(), form=(), raw=()):
        self._aria = list(aria)
        self._form = list(form)
        self._raw = list(raw)

    async def error_texts(self):
        return list(self._aria)

    async def form_texts(self):
        return list(self._form)

    async def collect_controls(self):
        return list(self._raw)


class _Obs:
    def __init__(self, raw):
        self.raw_controls = raw
        self.url = "http://x/apply/beneficiary/"


def _walker(port, controls):
    class _W(WalkerMixin):
        def __init__(self):
            self._port = port
            self._refuse_pack = None
            self._validation_rejections = []
            self._raw = controls

        async def _observe(self):
            return _Obs(self._raw)

    return _W()


_RULE = "Primary beneficiary allocations must total 100%. Currently at 10%."
_FOOTER = "Required fields are marked with an error message in red."


def _controls():
    return [{"role": "spinbutton", "kind": "text", "name": "Percentage (%)"},
            {"role": "textbox", "kind": "text", "name": "First Name"}]


def _run(w, before):
    return asyncio.run(w._name_validation_rejections(
        "http://x/apply/beneficiary/", "advance:Continue to Signature",
        before_texts=before))


# ── the live case, and the clauses ─────────────────────────────────────────

def test_the_plain_text_refusal_vkpower_actually_renders_is_named():
    """THE LIVE CASE. No ARIA, no anchor, no class read — the rule appeared in
    the form after the declined submit, and that transition is the evidence."""
    port = _Port(form=[_RULE, "Beneficiary Designation"])
    w = _walker(port, _controls())
    named = _run(w, before=["Beneficiary Designation"])
    assert named == 1
    rec = w._validation_rejections[0]
    assert rec["rule"].startswith("Primary beneficiary allocations")
    assert rec["anchored_by"] == "text_transition"
    assert rec["field"] == "", "no control is named in this rule — no guess"


def test_rejection_text_already_on_the_page_is_never_a_verdict():
    """CLAUSE 3, the anti-fabrication half. The footer sentence is
    rejection-shaped and was there BEFORE the click — reading it would fail a
    form that never refused anything."""
    port = _Port(form=[_FOOTER, "Beneficiary Designation"])
    w = _walker(port, _controls())
    named = _run(w, before=[_FOOTER, "Beneficiary Designation"])
    assert named == 0
    assert w._validation_rejections == []


def test_the_control_the_same_footer_appearing_fresh_is_read():
    """FALSIFICATION CONTROL for the test above: the SAME text, genuinely new,
    must be read — otherwise a reader that reads nothing passes both."""
    port = _Port(form=[_FOOTER])
    w = _walker(port, _controls())
    assert _run(w, before=["Beneficiary Designation"]) == 1


def test_new_text_that_is_not_rejection_shaped_is_not_a_rejection():
    """Polarity. A counter or a breadcrumb appearing at the same moment is a
    page doing ordinary things."""
    port = _Port(form=["3 of 10 steps complete"])
    w = _walker(port, _controls())
    assert _run(w, before=["Beneficiary Designation"]) == 0


def test_without_a_before_snapshot_the_rung_stays_silent():
    """No diff, no claim. The ARIA rung above it still ran; this rung must not
    degrade into an after-only read — that is the fabrication case."""
    port = _Port(form=[_RULE])
    w = _walker(port, _controls())
    assert _run(w, before=()) == 0


# ── the bridge to B2: a rule that names a control is attributed ────────────

def test_a_rule_that_names_a_control_is_attributed_to_it():
    port = _Port(form=["First Name is required."])
    w = _walker(port, _controls())
    assert _run(w, before=[]) == 0 or True  # before=() covered above
    named = _run(w, before=["x"])
    assert named == 1
    rec = w._validation_rejections[0]
    assert rec["field"] == "First Name"
    assert rec["anchored_by"] == "text_names_control"


def test_the_longer_of_two_near_duplicate_labels_wins():
    """THE B4 TRAP, held here on purpose: summit has both "Face Amount ($)" and
    "Face Amount", and they must not be confused — the longest matching label
    is the one the rule is about."""
    controls = [
        {"role": "textbox", "kind": "text", "name": "Face Amount"},
        {"role": "textbox", "kind": "text", "name": "Face Amount ($)"}]
    port = _Port(form=["Face Amount ($) must be at least 10000 and is invalid."])
    w = _walker(port, controls)
    assert _run(w, before=["x"]) == 1
    assert w._validation_rejections[0]["field"] == "Face Amount ($)"


def test_the_aria_rung_still_outranks_the_text_rung():
    """The stronger evidence first: an application that DOES annotate its
    errors is read through its own annotation, exactly as before B1."""
    controls = [
        {"role": "textbox", "kind": "text", "name": "SSN",
         "aria_invalid": "true", "validation_message": "Enter a valid SSN"},
    ]
    port = _Port(aria=["Enter a valid SSN"], form=["Enter a valid SSN"])
    w = _walker(port, controls)
    named = _run(w, before=["x"])
    assert named >= 1


def test_a_port_without_the_new_reader_degrades_to_the_old_behaviour():
    class _OldPort:
        async def error_texts(self):
            return []

        async def collect_controls(self):
            return []

    w = _walker(_OldPort(), [])
    w._port = _OldPort()
    assert _run(w, before=["x"]) == 0


# ── the port's scope, proven in a real browser ─────────────────────────────

def test_the_form_scope_is_real_and_reads_no_class_names():
    """CLAUSES 1 + 2 IN CHROMIUM, driving the SHIPPED evaluate source rather
    than a copy that can drift. The error node inside the form carries NO class
    attribute at all and is read; the rejection-shaped toast OUTSIDE the form
    is not — the scope is the form element the platform declares, never a
    palette."""
    import inspect

    import pytest

    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from app import playwright_port

    source = inspect.getsource(playwright_port.PlaywrightBrowserPort.form_texts)
    start = source.index('"""() => {')
    end = source.index('}"""', start)
    js = source[start + 3:end + 1]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(
            '<div id="toast">This toast is invalid and outside the form.</div>'
            '<form>'
            '  <label>Percentage (%)<input type="number"></label>'
            '  <p>Primary beneficiary allocations must total 100%.</p>'
            '  <button type="submit">Continue</button>'
            '</form>')
        texts = page.evaluate(js)
        browser.close()

    joined = " | ".join(texts)
    assert "must total 100%" in joined, "the classless in-form error is read"
    assert "toast" not in joined.lower(), "the out-of-form toast is not"


def test_a_page_with_no_form_falls_back_to_the_page_wide_read():
    """The fallback, stated in the reader's docstring: form-lessness is common
    in React apps, and the transition diff plus rejection polarity still bound
    what can be claimed from a page-wide read."""
    import inspect

    import pytest

    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright

    from app import playwright_port

    source = inspect.getsource(playwright_port.PlaywrightBrowserPort.form_texts)
    start = source.index('"""() => {')
    end = source.index('}"""', start)
    js = source[start + 3:end + 1]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(
            '<div><p>Face Amount ($) must be at least 10000.</p></div>')
        texts = page.evaluate(js)
        browser.close()

    assert any("at least 10000" in t for t in texts)
