"""THE M0.3 GATE — every extraction must leave these bytes untouched.

Each fixture is crawled end-to-end through the deterministic harness and its
manifest is compared, byte for byte, against a golden captured before the
decomposition began.  A diff here is not a test failure to be negotiated with;
it is proof that an extraction changed behaviour, and the extraction is wrong.

REGENERATING.  ``QEC_UPDATE_GOLDENS=1 pytest tests/test_characterization.py``
rewrites the goldens.  It exists so the baseline can be MINTED once, and for
the rare case where a golden must move because production behaviour was
deliberately changed by some other, non-M0.3 work.  Running it to make a red
gate go green during an extraction defeats the entire milestone.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.characterization import harness
from tests.characterization.fixtures import ALL_FIXTURES
from tests.characterization.harness import golden_path, run_fixture

_UPDATE = os.environ.get("QEC_UPDATE_GOLDENS") == "1"


@pytest.mark.parametrize("fixture", ALL_FIXTURES, ids=lambda f: f.name)
def test_crawl_is_byte_identical_to_golden(fixture, tmp_path, monkeypatch):
    work_dir = tmp_path / "qec_char_work"
    work_dir.mkdir()

    actual, _digest = run_fixture(fixture, work_dir, monkeypatch)

    path = golden_path(fixture.name)
    if _UPDATE or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8", newline="\n")
        if not _UPDATE:
            pytest.fail(
                f"golden {path.name} did not exist and has been MINTED. "
                "Re-run to gate against it.")
        return

    expected = path.read_text(encoding="utf-8")
    if actual == expected:
        return

    # A readable first-divergence report: a 3.000-line unified diff tells you
    # nothing useful about which RECORD moved.
    a_lines, e_lines = actual.splitlines(), expected.splitlines()
    for i, (got, want) in enumerate(zip(a_lines, e_lines)):
        if got != want:
            pytest.fail(
                f"{fixture.name}: manifest diverged at record {i + 1}\n"
                f"  expected: {want[:400]}\n"
                f"  actual  : {got[:400]}")
    pytest.fail(
        f"{fixture.name}: manifest length changed "
        f"({len(e_lines)} golden records -> {len(a_lines)} actual)")


def test_every_fixture_has_a_golden():
    """A fixture without a golden is an unguarded crawl path."""
    missing = [f.name for f in ALL_FIXTURES if not golden_path(f.name).exists()]
    assert not missing, f"fixtures with no golden baseline: {missing}"


def test_the_harness_freezes_every_reader_of_the_wall_calendar():
    """No unfrozen entropy source may re-enter the harness.

    Found the hard way: goldens minted one day failed the next, diverging by a
    single character — a synthesized date-of-birth one day older. Nothing had
    regressed; the calendar had moved. A gate that reddens by itself overnight
    teaches people to re-mint instead of investigate, which silently destroys
    the only instrument this milestone is measured with.

    So the harness pins ``date.today()``, and THIS test pins the pinning: any
    app module that reads the wall calendar must be declared in the harness's
    ``_DATE_CONSUMERS``, or the next one added re-opens the hole.
    """
    app_dir = Path(harness.__file__).resolve().parents[2] / "app"
    frozen = {m.__name__.rsplit(".", 1)[-1] for m in harness._DATE_CONSUMERS}

    readers = set()
    for path in sorted(app_dir.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "date.today()" in text or "datetime.now(" in text:
            readers.add(path.stem)

    unfrozen = readers - frozen
    assert not unfrozen, (
        "these app modules read the wall calendar but the characterization "
        f"harness does not freeze them: {sorted(unfrozen)}. Add them to "
        "harness._DATE_CONSUMERS (and re-mint only if the goldens genuinely "
        "change).")
