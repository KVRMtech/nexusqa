"""File-upload GENERATION (#8, factory half) — founder-approved factory edits.

Pins the new end-to-end path for a RECORDED upload (crawler verb=upload, value =
the browser-committed chosen filename): the generator emits a grounded "Attach"
step, and the compiler emits a REAL `setInputFiles` via the gated `__nxSetFiles`
helper with an attach oracle — never a `fill()` on a file input, and never any
upload scaffolding in a suite without upload steps (byte-identical rule).

NO live stack / NO DB / NO SDK: nexus_sdk is stubbed when absent and modules are
loaded by path (same approach as test_generator_navigation_grounding.py).
Run from Nexus_power/platform/api:
    python -m pytest tests/test_upload_generation.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types

# ── nexus_sdk stub (only when the real package is absent) ────────────────────
try:
    from nexus_sdk.models import ProductionTestCase, ProductionTestStep  # noqa: F401
except Exception:
    class _Base:
        def __init__(self, **kw):
            for k, v in kw.items():
                setattr(self, k, v)

    class Precondition(_Base):
        pass

    class ProductionTestStep(_Base):
        def __init__(self, **kw):
            self.observed = {}
            self.provenance = ""
            self.screenshot = ""
            self.data_ref = None
            super().__init__(**kw)

    class ProductionTestCase(_Base):
        pass

    _mod = types.ModuleType("nexus_sdk")
    _models = types.ModuleType("nexus_sdk.models")
    _models.Precondition = Precondition
    _models.ProductionTestStep = ProductionTestStep
    _models.ProductionTestCase = ProductionTestCase
    _mod.models = _models
    sys.modules["nexus_sdk"] = _mod
    sys.modules["nexus_sdk.models"] = _models

_APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")

# ── generator: load by file path (pure logic) ────────────────────────────────
_GEN_PATH = os.path.join(_APP, "test_factory", "generator.py")
_spec = importlib.util.spec_from_file_location("nexus_generator_upload_ut", _GEN_PATH)
gen = importlib.util.module_from_spec(_spec)
sys.modules["nexus_generator_upload_ut"] = gen
_spec.loader.exec_module(gen)
PV, PA = gen.PageVisitInput, gen.PageActionInput

# ── compiler: load as a SYNTHETIC package so its relative imports resolve ────
_svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
_sf = types.ModuleType("svc.script_factory")
_sf.__path__ = [os.path.join(_APP, "script_factory")]
_tf = types.ModuleType("svc.test_factory")
_tf.__path__ = [os.path.join(_APP, "test_factory")]
sys.modules.setdefault("svc", _svc)
sys.modules.setdefault("svc.script_factory", _sf)
sys.modules.setdefault("svc.test_factory", _tf)
compiler = importlib.import_module("svc.script_factory.compiler")


def _visits():
    return [
        PV(page_visit_id="v1", sequence_index=0, location="Home",
           url_host="app.example", url_path="/", url_query="",
           canonical_host="app.example", source="ground_truth", form_snapshot={}),
        PV(page_visit_id="v2", sequence_index=1, location="Contact",
           url_host="app.example", url_path="/contact", url_query="",
           canonical_host="app.example", source="ground_truth", form_snapshot={}),
    ]


def _upload_actions():
    return [
        PA(page_visit_id="v1", subaction_index=0, verb="click",
           target_label="Contact", target_kind="link", value=None,
           after_outcome="navigation", navigated=True),
        PA(page_visit_id="v2", subaction_index=0, verb="type",
           target_label="Subject", target_kind="text_field", value="Warranty"),
        PA(page_visit_id="v2", subaction_index=1, verb="upload",
           target_label="Attachment", target_kind="text_field",
           value="C:\\fakepath\\qec-seed-abc123.pdf",
           after_outcome="value_committed"),
    ]


def _case(actions=None):
    res = gen.generate_demonstrated_test_cases(
        artifact_id="t", page_visits=_visits(),
        page_actions=actions if actions is not None else _upload_actions())
    assert res.test_cases, "expected a demonstrated case"
    return res.test_cases[0]


# ─── generator half ───────────────────────────────────────────────────────────


def test_upload_action_becomes_a_grounded_attach_step():
    steps = _case().steps
    attach = [s for s in steps if "Attach" in (getattr(s, "action", "") or "")]
    assert len(attach) == 1
    st = attach[0]
    obs = st.observed or {}
    assert obs.get("verb") == "upload"
    assert obs.get("kind") == "file"
    # the fakepath mask is stripped — only the honest basename is carried.
    assert obs.get("value") == "qec-seed-abc123.pdf"
    assert "fakepath" not in (st.action or "")
    assert st.data_ref == "qec-seed-abc123.pdf"


def test_valueless_or_nameless_upload_is_skipped_honestly():
    acts = _upload_actions()
    acts[2] = PA(page_visit_id="v2", subaction_index=1, verb="upload",
                 target_label="", target_kind="text_field",
                 value="C:\\fakepath\\x.pdf", after_outcome="value_committed")
    steps = _case(acts).steps
    assert not [s for s in steps if "Attach" in (getattr(s, "action", "") or "")]


def test_duplicate_upload_across_split_visits_is_deduped():
    acts = _upload_actions() + [
        PA(page_visit_id="v2", subaction_index=2, verb="upload",
           target_label="Attachment", target_kind="text_field",
           value="C:\\fakepath\\qec-seed-abc123.pdf",
           after_outcome="value_committed"),
    ]
    steps = _case(acts).steps
    attach = [s for s in steps if "Attach" in (getattr(s, "action", "") or "")]
    assert len(attach) == 1


# ─── compiler half ────────────────────────────────────────────────────────────


def _compile(tc):
    return compiler.compile_case(tc)


def test_upload_step_compiles_to_setinputfiles_with_attach_oracle():
    spec = _compile(_case())
    # gated helper is defined exactly once and used with BOTH locators + basename.
    assert spec.count("async function __nxSetFiles") == 1
    assert ("await __nxSetFiles(page.getByLabel('Attachment'), "
            "page.locator('input[type=\"file\"]'), 'qec-seed-abc123.pdf');") in spec
    # the grounded attach oracle proves the input holds a file, BOUNDED + labeled
    # (fails RED in ~10s on a wrong bind or a rejected attach, never hangs).
    assert "el.files && el.files.length" in spec
    assert ".toPass({ timeout: 10000 });" in spec
    # NEVER a text-fill on the file input.
    assert "fileInput.fill(" not in spec


def test_upload_bind_is_fail_closed_never_a_dom_order_union():
    """Adversarial regression (green-wash hole): the structural input[type=file]
    rung must NEVER compete with the label rung in a .or() union (DOM order could
    silently bind a DIFFERENT file input on a multi-upload page). The helper binds
    label-first, falls back ONLY to an unambiguous single file input, else THROWS."""
    spec = _compile(_case())
    # the helper embodies the fail-closed ladder:
    assert "upload bind refused" in spec          # the honest ambiguity throw
    assert "if (n === 1)" in spec                 # unambiguous-single fallback only
    # the upload bind is NOT a .or() union of label + structural rungs:
    assert "getByLabel('Attachment').or(" not in spec
    # and the attach step carries NO false "UNVERIFIED expected result" note —
    # it carries its own grounded attach oracle.
    _attach_zone = spec[spec.index("__nxSetFiles(page.getByLabel"):]
    _attach_zone = _attach_zone[:_attach_zone.index("test.step") if "test.step" in _attach_zone else len(_attach_zone)]
    assert "UNVERIFIED expected result" not in _attach_zone


def test_hostile_filename_cannot_break_the_spec():
    """Adversarial regression: a newline/quote-carrying recorded filename must not
    break out of the emitted JS or split a // comment onto a new line (which would
    parse-fail the WHOLE spec)."""
    acts = _upload_actions()
    acts[2] = PA(page_visit_id="v2", subaction_index=1, verb="upload",
                 target_label="Attachment", target_kind="text_field",
                 value="C:\\fakepath\\line\nbreak'); await page.close(); //.pdf",
                 after_outcome="value_committed")
    spec = _compile(_case(acts))
    # every emitted line stays a single line (no raw newline injection) and the
    # payload never appears unescaped.
    assert "await page.close()" not in spec.replace("\\'", "")
    for line in spec.splitlines():
        assert not line.strip().startswith("break")  # no orphaned fragment lines


def test_multiline_observed_outcome_cannot_break_the_spec():
    """Adversarial regression (the live daa45c67 defect): a captured observation that
    carries a MULTI-LINE Playwright error ('... Timeout 5000ms exceeded.\\nCall log:\\n
    waiting for get_by_role(...)') must be COLLAPSED onto the single ``// observed
    outcome:`` comment line. Emitted raw, its later lines parse as CODE ('Call log:' →
    Missing semicolon), the WHOLE spec fails to compile, Playwright runs 0 tests, and
    the operator sees no evidence while the live browser is stranded on some page."""
    tc = _case()
    steps = list(tc.steps)
    forged = ProductionTestStep(
        step_number=len(steps) + 1, action="Click 'X'",
        expected="", expected_result="", selector="",
    )
    forged.observed = {
        "verb": "click", "label": "X",
        "after": ("action_error: Locator.click: Timeout 5000ms exceeded.\nCall log:\n"
                  "waiting for get_by_role(\"link\", name=\"feedback@x.com\").first\n})"),
    }
    steps.append(forged)
    tc.steps = steps
    spec = _compile(tc)
    # the whole error is collapsed onto ONE observed-outcome comment line:
    oc = [ln for ln in spec.splitlines() if "observed outcome:" in ln]
    assert any("Call log:" in ln and "waiting for" in ln for ln in oc), \
        "the multi-line error must be collapsed onto a single observed-outcome comment"
    # and NO fragment leaked as a bare (uncommented) code line:
    for line in spec.splitlines():
        st = line.strip()
        assert not st.startswith("Call log:"), f"leaked as code: {line!r}"
        assert not st.startswith("waiting for"), f"leaked as code: {line!r}"


def test_suite_without_uploads_carries_no_upload_scaffolding():
    acts = [a for a in _upload_actions() if a.verb != "upload"]
    spec = _compile(_case(acts))
    assert "__nxSetFiles" not in spec
    assert "setInputFiles" not in spec


def test_unknown_verbs_still_demote_to_inert_comment():
    tc = _case()
    # forge an out-of-vocabulary verb on a copy of the attach step
    steps = list(tc.steps)
    forged = ProductionTestStep(
        step_number=len(steps) + 1, action="Do something exotic",
        expected="", expected_result="", selector="",
    )
    forged.observed = {"verb": "teleport", "label": "X"}
    steps.append(forged)
    tc.steps = steps
    spec = _compile(tc)
    assert "// (no executable action derived)" in spec


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print("ALL PASS" if not failed else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
