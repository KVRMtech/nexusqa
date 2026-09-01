"""T-HN-04 — FIXTURE APPLICATION LIBRARY: structural validation.

The fixture library is the harness's evidence base. If a fixture is
undocumented, unreachable, self-contradictory or silently asserting nothing,
every conclusion drawn from it is worth less than it appears. These tests
validate the library itself rather than the capture engine.

Each fixture must:
  * exist as an independently servable app (`index.html`),
  * declare its contract (`expected.json`),
  * document purpose / expected controls / expected manifest / targeted defect
    and how to run it alone (`README.md`),
  * be reachable over HTTP and actually assert something in at least one lane.
"""
from __future__ import annotations

import json

import pytest

import _harness as H

pytestmark = [pytest.mark.browser]

#: The milestone names thirteen capabilities; the library must hold all of them.
REQUIRED_CAPABILITIES = [
    "shadow-open", "shadow-closed", "iframe-same-origin", "iframe-cross-origin",
    "aria-labelledby-multiblock", "custom-listbox", "native-select-250",
    "radio-groups", "questionnaire-20-samefingerprint", "save-draft-wizard",
    "confirm-gated-step", "download-step", "canvas",
]


def test_all_thirteen_capabilities_are_present() -> None:
    """The milestone names thirteen capabilities; each needs exactly one fixture.

    Asserted as a REQUIRED SET rather than an exact count: the library is meant
    to grow, and a fourteenth fixture is a contribution, not a violation. Every
    fixture — required or added — is still held to the documentation and
    contract tests below, so growth cannot dilute the standard.
    """
    names = H.fixture_names()
    assert len(names) >= 13, f"expected at least 13 fixtures, found {len(names)}: {names}"
    for capability in REQUIRED_CAPABILITIES:
        matching = [n for n in names if n.endswith(capability)]
        assert len(matching) == 1, (
            f"capability {capability!r} is not covered by exactly one fixture "
            f"(found {matching})")


def test_no_fixture_is_half_written() -> None:
    """A fixture directory must carry both its app and its contract.

    Discovery skips incomplete directories so that one mid-edit fixture cannot
    crash every parametrised test in the suite. That tolerance would become a
    hiding place if nothing reported it, so the report is this test: exactly one
    named failure, naming the directory and the missing file.
    """
    incomplete = H.incomplete_fixture_names()
    assert not incomplete, (
        "these fixture directories are incomplete and are therefore NOT being "
        "executed by any lane:\n" + "\n".join(
            f"  {name}: missing {', '.join(missing)}"
            for name, missing in incomplete.items()))


def test_every_fixture_is_documented(fixture_name) -> None:
    """A fixture nobody can interpret is not evidence."""
    readme = H.FIXTURES_DIR / fixture_name / "README.md"
    assert readme.exists(), f"{fixture_name} has no README.md"
    text = readme.read_text(encoding="utf-8")

    for heading in ("## Purpose", "## Expected controls", "## Expected manifest",
                    "## Targeted defect", "## Running this fixture alone"):
        assert heading in text, f"{fixture_name}/README.md is missing {heading!r}"

    assert len(text) > 600, (
        f"{fixture_name}/README.md is {len(text)} chars — too thin to explain "
        f"what the fixture proves")


def test_every_fixture_declares_a_valid_contract(fixture_name) -> None:
    spec = H.fixture_spec(fixture_name)

    for key in ("purpose", "targeted_defect", "lanes", "snippet", "min_controls"):
        assert key in spec, f"{fixture_name}/expected.json is missing {key!r}"

    assert spec["lanes"], f"{fixture_name} declares no lanes"
    assert set(spec["lanes"]) <= {"jsdom", "playwright"}, spec["lanes"]
    assert spec["snippet"] in ("INVENTORY_JS", "OPAQUE_JS", "DISPLAYED_VALUES_JS")
    assert isinstance(spec["min_controls"], int) and spec["min_controls"] >= 0

    # A fixture that is neither a regression guard nor a bug reproduction has no
    # stated reason to exist. Three accepted openings, because there turned out
    # to be three real kinds:
    #   BUG-…            a reproduction of a named defect
    #   None…            a guard on behaviour that was always correct
    #   Regression guard a guard on behaviour a PREVIOUS FIX introduced or
    #                    endangered — e.g. fixture 17, where scoping name
    #                    resolution to the owning shadow root also scoped
    #                    groupContainerKey's positional lookup and merged two
    #                    anonymous radiogroups into one question.
    # The third was added when a fixture needed it; the rule's intent is that a
    # fixture states WHY it exists, not that it uses one of two magic words.
    defect = spec["targeted_defect"]
    assert defect.startswith(("BUG-", "None", "Regression guard")), (
        f"{fixture_name} must state why it exists: name the BUG- it reproduces, "
        f"or open with 'None (regression guard)' / 'Regression guard …'; "
        f"got {defect[:60]!r}")

    # A fixture restricted to one lane must say why, or the restriction reads as
    # an oversight and someone will "helpfully" widen it.
    if len(spec["lanes"]) == 1:
        assert spec.get("lane_note"), (
            f"{fixture_name} is restricted to {spec['lanes']} but gives no "
            f"lane_note explaining why the other lane cannot adjudicate it")

    # It must assert SOMETHING.
    assertions = (len(spec.get("expect_controls", []))
                  + len(spec.get("forbid_controls", []))
                  + len(spec.get("describes_correct_behaviour", []))
                  + len(spec.get("describes_runtime_behaviour", [])))
    assert assertions > 0, f"{fixture_name} declares no expectations at all"


def test_runtime_behaviour_blocks_are_well_formed(fixture_name) -> None:
    """``describes_runtime_behaviour`` states behaviour that IS implemented.

    It exists because the older ``describes_correct_behaviour`` means the exact
    opposite — correct-but-UNIMPLEMENTED — and ``test_known_bugs`` reproduces
    every entry of it as a control-shape assertion. Declaring working behaviour
    under that key does not merely mis-file it: it asserts a control shape that
    was never written, so the fixture reports a bug that does not exist. The two
    blocks are therefore kept apart by name and by shape, and a fixture may not
    put the same claim in both.
    """
    spec = H.fixture_spec(fixture_name)
    cases = spec.get("describes_runtime_behaviour", [])
    if not cases:
        return
    assert "describes_correct_behaviour" not in spec, (
        f"{fixture_name} declares BOTH behaviour blocks; one means implemented "
        f"and the other means not, so a reader cannot tell which claim is which")
    for case in cases:
        assert str(case.get("behaviour") or "").strip(), (
            f"{fixture_name}/describes_runtime_behaviour has an entry with no "
            f"'behaviour' prose: {case}")
        assert "where" not in case and "fields" not in case, (
            f"{fixture_name}/describes_runtime_behaviour entry {case} carries a "
            f"control shape — that is the describes_correct_behaviour contract, "
            f"and this block is adjudicated by a runtime test, not by capture")
    assert spec.get("lane_note"), (
        f"{fixture_name} declares runtime behaviour but no lane_note saying "
        f"which test adjudicates it")


def test_every_fixture_is_independently_servable(fixture_server, fixture_name) -> None:
    """Each fixture must be reachable on its own URL, with no shared setup."""
    import urllib.request

    url = fixture_server.url(fixture_name)
    with urllib.request.urlopen(url, timeout=10) as resp:
        assert resp.status == 200, f"{fixture_name} served {resp.status}"
        body = resp.read().decode("utf-8")
    assert "<html" in body.lower(), f"{fixture_name} did not serve HTML"
    assert "__ALT_ORIGIN__" not in body, (
        f"{fixture_name} still contains an unsubstituted origin token — the "
        f"cross-origin embed would resolve to a relative path instead")


#: Every key ``_harness.assert_control`` reads, plus the documentation keys the
#: fixture library already uses. Kept beside the test that enforces it.
_CONTROL_SPEC_KEYS = frozenset({
    "where", "fields", "list_lengths", "list_edges", "href_suffix",  # consumed
    "lanes",                                                          # runs_in_lane
    "why", "lane_note",                                               # prose
})


def test_control_expectations_carry_a_where_clause(fixture_name) -> None:
    """``expect_controls`` / ``forbid_controls`` entries must be MATCHABLE.

    THE ESCAPE THIS CLOSES.  ``assert_control`` opens with an unguarded
    ``spec["where"]``, so an entry written in the wrong shape does not fail as a
    contract violation — it fails as a bare ``KeyError: 'where'`` from inside the
    harness, in the Chromium lane, forty minutes into a seventy-minute run, with a
    traceback that points at the harness rather than at the fixture that is
    actually wrong.

    Fixture 30 shipped in exactly that state.  Its four entries were flat
    ``{"name": …, "kind": …}`` dicts rather than ``{"where": {…}, "fields": {…}}``,
    and every other contract test passed it: the fixture declares its purpose, its
    lanes, its snippet, and it asserts SOMETHING, which is all
    ``test_every_fixture_declares_a_valid_contract`` measures.  Nothing looked at
    the shape of the entries themselves.

    The inverse rule already existed one function up —
    ``describes_runtime_behaviour`` is checked for NOT carrying a control shape —
    so the positive half was simply missing.  It runs in the jsdom lane, needs no
    browser, and answers in milliseconds.
    """
    spec = H.fixture_spec(fixture_name)
    for block in ("expect_controls", "forbid_controls"):
        for i, item in enumerate(spec.get(block, [])):
            where = item.get("where")
            assert isinstance(where, dict) and where, (
                f"{fixture_name}/{block}[{i}] has no usable 'where' mapping: "
                f"{item}" + chr(10) +
                f"Control expectations are {{'where': {{<captured fields to match "
                f"on>}}, 'fields': {{<fields to assert>}}}}. Without it "
                f"assert_control raises KeyError from inside the harness and the "
                f"failure names the harness instead of this fixture.")
            # The allowed vocabulary is DERIVED from what assert_control actually
            # reads (`where`, `fields`, `list_lengths`, `list_edges`,
            # `href_suffix`) plus the two documentation keys the library already
            # uses (`why`, `lane_note`) and the lane narrowing `runs_in_lane`
            # honours. It is written down here rather than inferred at runtime so
            # that ADDING an assertion kind to the harness is a deliberate two-line
            # change instead of something a fixture can invent silently.
            #
            # This half of the guard was very nearly shipped with an allow-list I
            # had guessed instead of read, which red-flagged eighteen correct
            # fixtures over `why`, `href_suffix` and `list_lengths` — all three of
            # which the harness genuinely consumes.
            unknown = sorted(k for k in item if k not in _CONTROL_SPEC_KEYS)
            assert not unknown, (
                f"{fixture_name}/{block}[{i}] carries unrecognised key(s) "
                f"{unknown}. assert_control reads only {sorted(_CONTROL_SPEC_KEYS)}, "
                f"so a misspelled key is silently ignored and the expectation "
                f"passes while asserting nothing.")


def test_expectations_are_lane_reachable(fixture_name) -> None:
    """No expectation may be declared in a lane the fixture does not run in."""
    spec = H.fixture_spec(fixture_name)
    fixture_lanes = set(spec["lanes"])
    for block in ("expect_controls", "forbid_controls",
                  "describes_correct_behaviour", "describes_runtime_behaviour",
                  "already_correct"):
        for item in spec.get(block, []):
            lanes = set(item.get("lanes") or fixture_lanes)
            assert lanes <= fixture_lanes, (
                f"{fixture_name}/{block} declares lanes {sorted(lanes)} but the "
                f"fixture only runs in {sorted(fixture_lanes)} — this "
                f"expectation would never execute")
            assert lanes, f"{fixture_name}/{block} has an expectation with no lane"


def test_frame_selector_recipe_is_fully_exercised(pw, fixture_server) -> None:
    """Every rung of `frameSelectorFor()` must be reached by fixture 03.

    The recipe is a five-rung fallback chain (id → name → title → src →
    positional) and the two DEFECTIVE rungs sit in the middle of it. A fixture
    covering only the first rung would report the recipe healthy.
    """
    spec = H.fixture_spec("03-iframe-same-origin")
    recipe = spec["frame_selector_recipe"]
    controls = pw.collect(fixture_server.url("03-iframe-same-origin"))
    selectors = {c["frame_selector"] for c in controls}

    assert len(selectors) == recipe["expect_distinct_frame_selectors"], (
        f"expected {recipe['expect_distinct_frame_selectors']} distinct frame "
        f"selectors (main frame + 5 rungs), got {len(selectors)}: {sorted(selectors)}")

    rungs = {
        "id": lambda s: s.startswith("iframe#"),
        "name": lambda s: s.startswith("iframe[name="),
        "title": lambda s: s.startswith("iframe[title="),
        "src": lambda s: s.startswith("iframe[src="),
        "positional": lambda s: s.startswith("iframe >> nth="),
    }
    unreached = [rung for rung, match in rungs.items()
                 if not any(match(s) for s in selectors)]
    assert not unreached, (
        f"these frameSelectorFor() rungs were never reached: {unreached}. "
        f"Captured: {sorted(selectors)}")


def test_the_walker_has_no_uncallable_functions() -> None:
    """Every function defined in the walker must have at least one call site.

    Coverage percentage cannot distinguish "this branch is untested" from "this
    branch is unreachable", and dead code silently caps the achievable number
    forever. This test makes the distinction structurally.

    It found and removed one: `optionsOf(el)`, a one-line wrapper returning
    `optionsAndTotalOf(el).list`. Nothing called it — `describe()` reads
    `optionsAndTotalOf` directly because it needs the total as well as the list —
    so it read as a second option path that did not exist.
    """
    import re

    js = H.production_snippet("INVENTORY_JS")
    defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", js))
    assert defined, "no functions found — the extraction is broken, not the code"

    uncalled = []
    for name in sorted(defined):
        # Every occurrence of `name(` minus the one in `function name(`.
        call_sites = len(re.findall(rf"\b{re.escape(name)}\s*\(", js)) - 1
        if call_sites <= 0:
            uncalled.append(name)

    assert not uncalled, (
        f"these walker functions are defined but never called, so no fixture can "
        f"ever cover them and the coverage ceiling is permanently below 100%: "
        f"{uncalled}. Either call them or delete them.")


def test_the_fixture_library_covers_the_walkers_capability_surface() -> None:
    """Every naming rung and control kind the walker implements has a fixture.

    Coverage percentage answers "did the code run"; this answers "did we mean
    to run it". A rung with no fixture is an untested capability even when V8
    happens to report the line as covered incidentally.
    """
    declared: set[str] = set()
    for name in H.fixture_names():
        spec = H.fixture_spec(name)
        for block in ("expect_controls", "describes_correct_behaviour",
                      "already_correct"):
            for item in spec.get(block, []):
                fields = item.get("fields") or {}
                if "name_source" in fields:
                    declared.add(f"name_source:{fields['name_source']}")
                if "role" in fields:
                    declared.add(f"role:{fields['role']}")

    required_rungs = {
        "name_source:label-for", "name_source:aria-labelledby",
        "name_source:aria-label", "name_source:wrapping-label",
        "name_source:content",
    }
    missing = required_rungs - declared
    assert not missing, f"accessible-name rungs with no fixture assertion: {sorted(missing)}"

    required_roles = {
        "role:textbox", "role:button", "role:link", "role:checkbox",
        "role:radio", "role:combobox", "role:listbox", "role:spinbutton",
    }
    missing_roles = required_roles - declared
    assert not missing_roles, (
        f"control roles with no fixture assertion: {sorted(missing_roles)}")
