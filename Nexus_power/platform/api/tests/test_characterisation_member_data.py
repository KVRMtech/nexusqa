"""CHARACTERISATION — the compiled-output contract the member-data work builds on.

These tests do not assert that the system is CORRECT. They assert what it does
TODAY, so that the phases which follow (resolve a value for the running member,
block when an answer is missing, drive per-member counts) prove they changed only
what they intended. A failure here is not automatically a bug — it means emitted
behaviour moved, and the diff must be looked at deliberately.

Two contracts are pinned:

  1. THE OVERRIDE SEAM. A parametrized fill emits ``(D['<key>'] ?? '<recorded>')``
     and the hard value oracle is gated on that same key being undefined. This is
     the seam the resolver phase feeds from the running member instead of from the
     request body. If the seam moves, the resolver silently stops applying.

  2. PERSONA-BLINDNESS. The compiler takes no persona, the persona bundle carries
     only credentials, and the per-member repetition helper is defined but never
     called. These are the exact facts the later phases change; pinning them makes
     each change visible rather than incidental.

NO live stack / NO DB. Run from Nexus_power/platform/api:
    python -m pytest tests/test_characterisation_member_data.py -q
"""
from __future__ import annotations

import importlib
import inspect
import os
import re
import sys
import types

import pytest

_APP = os.path.join(os.path.dirname(__file__), "..", "app", "services")

# compiler: load as a SYNTHETIC package so its relative imports resolve
# (same approach as tests/test_compiler_url_text_oracle.py)
_svc = types.ModuleType("svc"); _svc.__path__ = [_APP]
_sf = types.ModuleType("svc.script_factory")
_sf.__path__ = [os.path.join(_APP, "script_factory")]
sys.modules.setdefault("svc", _svc)
sys.modules.setdefault("svc.script_factory", _sf)
compiler = importlib.import_module("svc.script_factory.compiler")

from nexus_sdk.models import ProductionTestCase, ProductionTestStep  # noqa: E402

from app.services.test_factory import persona_store  # noqa: E402


# ── a case shaped exactly like a crawl-generated one ─────────────────────────
# Two committed fields: one whose value belongs to the member the crawl ran as,
# one that is ordinary test data. The compiler cannot currently tell them apart —
# that indistinguishability is the thing being pinned.

_IDENTITY_LABEL = "Member number"
_IDENTITY_VALUE = "8891234"
_DATA_LABEL = "Coverage amount"
_DATA_VALUE = "250000"
_TEST_ID = "tc-characterisation-1"


def _case() -> ProductionTestCase:
    return ProductionTestCase(
        test_id=_TEST_ID,
        name="Apply for cover",
        steps=[
            ProductionTestStep(
                step_number=1,
                action=f"Enter '{_IDENTITY_VALUE}' in the '{_IDENTITY_LABEL}' field",
                expected_result=f"'{_IDENTITY_LABEL}' shows '{_IDENTITY_VALUE}'",
                data_ref=_IDENTITY_VALUE,
                observed={"verb": "type", "label": _IDENTITY_LABEL,
                          "kind": "field", "value": _IDENTITY_VALUE},
            ),
            ProductionTestStep(
                step_number=2,
                action=f"Enter '{_DATA_VALUE}' in the '{_DATA_LABEL}' field",
                expected_result=f"'{_DATA_LABEL}' shows '{_DATA_VALUE}'",
                data_ref=_DATA_VALUE,
                observed={"verb": "type", "label": _DATA_LABEL,
                          "kind": "field", "value": _DATA_VALUE},
            ),
            ProductionTestStep(
                step_number=3,
                action="Click 'Continue'",
                expected_result="The application proceeds",
                observed={"verb": "click", "label": "Continue", "kind": "button"},
            ),
        ],
    )


@pytest.fixture(scope="module")
def spec() -> str:
    return compiler.compile_case(_case(), {}, parametrize=True)


# ── 1. the override seam the resolver phase will feed ────────────────────────

def test_override_key_is_a_slug_of_the_visible_label():
    """The seam is keyed on the field's LABEL, slugified — not on the slot name a
    credential card uses, and not on the value_key a per-member answer is stored
    under. Any resolver MUST translate into this key space or it silently no-ops."""
    assert compiler._data_key(_IDENTITY_LABEL) == "member-number"
    assert compiler._data_key(_DATA_LABEL) == "coverage-amount"
    # the shape, stated generically: lowercase, non-alphanumerics collapsed to '-'
    assert compiler._data_key("Policy No.") == "policy-no"
    assert compiler._data_key("  Date of Birth  ") == "date-of-birth"


def test_a_committed_field_reads_the_override_before_the_recorded_value(spec):
    """`(D['key'] ?? 'recorded')` — override wins, recorded value is the default."""
    assert f"(D['member-number'] ?? '{_IDENTITY_VALUE}')" in spec
    assert f"(D['coverage-amount'] ?? '{_DATA_VALUE}')" in spec


def test_the_override_map_prefers_per_test_over_global(spec):
    """D merges the shared defaults, then this test's own slot on top."""
    assert "__a['_global'] || {}" in spec
    assert f"__a['{_TEST_ID}'] || {{}}" in spec
    assert spec.index("__a['_global']") < spec.index(f"__a['{_TEST_ID}']")


def test_the_override_map_is_absent_when_a_case_commits_no_values():
    """parametrize is self-disabling: a case with nothing to fill emits no seam,
    so a resolver has nothing to attach to for read-only flows."""
    look_only = ProductionTestCase(
        test_id="tc-characterisation-2", name="Read only",
        steps=[ProductionTestStep(
            step_number=1, action="Click 'About'", expected_result="About is shown",
            observed={"verb": "click", "label": "About", "kind": "button"})],
    )
    assert "const D =" not in compiler.compile_case(look_only, {}, parametrize=True)


# ── 2. the oracle contract ───────────────────────────────────────────────────

def test_the_recorded_value_is_asserted_ONLY_when_no_override_is_active(spec):
    """The hard oracle is gated on the same key. With an override in play the
    recorded value is deliberately NOT asserted — which is what makes running as
    another member possible at all, and also what makes it silent today."""
    assert (f"if (D['member-number'] === undefined) "
            f"await expect(field).toHaveValue(/{_IDENTITY_VALUE}/i);") in spec
    assert (f"if (D['coverage-amount'] === undefined) "
            f"await expect(field).toHaveValue(/{_DATA_VALUE}/i);") in spec


def test_a_value_must_always_be_committed_regardless_of_override(spec):
    """The non-empty floor is unconditional — a no-op fill fails red even when an
    override is active. This must survive every later phase."""
    assert spec.count("await expect(field).not.toHaveValue('');") == 2


def test_the_token_oracle_is_swallowed(spec):
    """Deliberately tolerant so a data-driven run does not fail on formatting. It
    is therefore NOT a safety net: nothing here catches a wrong member's value."""
    assert "__nxTok(" in spec
    assert ".catch(() => {}); // grounded: field holds the entered value" in spec


def test_an_identity_value_is_indistinguishable_from_test_data(spec):
    """THE GAP, pinned. Both fields compile to the SAME emitted shape; nothing in
    the spec marks one as belonging to a person. Phase 1 (classification) exists to
    break this symmetry — when it does, this test should be revisited deliberately,
    not silently deleted.

    Read the two steps out of the spec itself and compare them with every literal
    blanked: what is left must be identical, character for character."""
    def emitted_step(label_slug: str) -> str:
        lines = [ln.strip() for ln in spec.splitlines()
                 if f"D['{label_slug}']" in ln]
        assert lines, f"no emitted lines found for {label_slug}"
        # blank every string literal and every regex body, leaving pure structure
        blanked = "\n".join(lines)
        blanked = re.sub(r"'[^']*'", "'X'", blanked)
        blanked = re.sub(r"/[^/\n]+/i", "/X/i", blanked)
        return blanked

    identity = emitted_step("member-number")
    ordinary = emitted_step("coverage-amount")
    assert identity == ordinary, (
        "the identity field and the ordinary data field no longer compile "
        "identically — classification may now be reaching the compiler")
    # and the emitted spec carries no notion of a member at all
    for marker in ("member_derived", "persona", "identity", "answer"):
        assert marker not in spec


# ── 3. persona-blindness, as it stands today ─────────────────────────────────

def test_the_compiler_accepts_no_persona_and_no_answers():
    """compile_case is persona-blind by construction. The resolver phase must
    therefore work at DISPATCH (through the data map), not by changing this
    signature — pinned so that constraint is not discovered late."""
    params = set(inspect.signature(compiler.compile_case).parameters)
    for forbidden in ("persona_id", "persona", "expected_values",
                      "answers", "classifications", "member_id"):
        assert forbidden not in params


def test_the_persona_bundle_carries_credentials_only():
    """build_persona_bundle is the whole persona -> run payload today: login
    choreography plus secrets. It carries no expected values, which is why a run
    currently asserts the crawl member's data."""
    recipe = {"login_path": "/login", "steps": [{"action": "goto", "path": "/login"}],
              "slots": [{"name": "member_number", "type": "secret"}]}
    auth_config, login_env = persona_store.build_persona_bundle(
        recipe, {"member_number": "8891234"})

    assert auth_config["strategy"] == "recipe"
    assert [s["name"] for s in auth_config["slots"]] == ["member_number"]
    # the secret rides the run env, keyed off the slot name
    assert login_env == {"NEXUS_LOGIN_MEMBER_NUMBER": "8891234"}
    # and nothing about what the member should SEE travels with it
    for key in ("expected_values", "answers", "classifications", "cardinality"):
        assert key not in auth_config


def test_no_credential_value_appears_in_the_auth_config():
    """A standing safety property, not merely a characterisation: secrets travel in
    the env, never in the config that can be written into a bundle."""
    recipe = {"login_path": "/login", "steps": [{"action": "fill", "slot": "password"}],
              "slots": [{"name": "password", "type": "secret"}]}
    auth_config, _ = persona_store.build_persona_bundle(recipe, {"password": "s3cr3t-v4lue"})
    assert "s3cr3t-v4lue" not in repr(auth_config)


def test_the_per_member_repeat_helper_is_defined_but_never_called(spec):
    """Counts are stored, planned and injected into the run env, yet the helper
    that would consume them is only defined in the auth-setup module and no spec
    calls it — which is why one suite cannot serve two differently-shaped members.
    Phase 5 makes this assertion flip."""
    assert "function __nxRepeat(" in compiler._AUTH_SETUP_TS
    assert "NEXUS_REPETITION" in compiler._AUTH_SETUP_TS
    assert "__nxRepeat(" not in spec


# ── 4. the login recipe replay shape ─────────────────────────────────────────

def test_the_recipe_interpreter_replays_the_recorded_shape():
    """The login steps a recording produces must remain replayable: the interpreter
    handles goto/fill/click/wait, treats a step marked optional as skippable, and
    asserts a reached Home rather than counting completed steps."""
    setup = compiler._AUTH_SETUP_TS
    for action in ("'goto'", "'fill'", "'click'", "'wait'", "'assert_home'"):
        assert action in setup
    assert "optional" in setup
    # login success is a STATE, and a failure to reach it is surfaced, not faked
    assert "homeAsserted" in setup
    assert "not fabricating a session" in setup


def test_recipe_slots_are_read_from_the_run_env_not_the_bundle():
    """Each slot is filled from its own env variable, so a bundle on disk never
    contains a member's credentials."""
    assert "NEXUS_LOGIN_" in compiler._AUTH_SETUP_TS
