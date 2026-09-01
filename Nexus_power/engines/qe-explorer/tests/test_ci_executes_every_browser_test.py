"""GATE 0 / A5 — a test file that CI never runs has proven nothing.

WHAT THIS PINS, AND WHY IT IS GREEN TODAY.  ``ci.yml``'s ``qe-explorer-browser``
job hands pytest the whole DIRECTORY::

    pytest tests/browser -m "not characterization" -q --tb=short

so every file under ``tests/browser`` is executed today, including the seven
that no workflow names one-by-one.  This test passes for that reason, and it is
worth being explicit that it is not decoration: the property is currently held
by a single unquoted directory argument in one job, and nothing else in the
repository states that the property is load-bearing.

THE ESCAPE IT CLOSES.  ``browser-harness.yml`` takes the opposite approach and
names its files individually::

    run: python -m pytest tests/browser/test_playwright_execution.py -v
    run: python -m pytest tests/browser/test_known_bugs.py tests/browser/test_capture_contract.py -v

That is a deliberate and good choice for that workflow -- a step per concern
means a red build names its own cause.  It also means those steps drifted: seven
files carrying the M1.5, M2.2, M2.5, M2.6, M3.1 and M3.2 browser proofs are
absent from every step in it.  They survive only because the OTHER workflow uses
a directory.  Narrow that one argument -- shard it by path, add a ``-k``, split
the job, or retire ``ci.yml``'s browser job in favour of the dedicated harness --
and seven files stop being executed with no red build anywhere to say so.

So this guard asserts the weakest true thing that would catch that: every
``tests/browser/test_*.py`` is either named by a workflow or covered by a bare
directory argument.  It runs in the fast engine lane, so it answers in
milliseconds instead of after the 45-minute browser job.

DELIBERATELY NAME-BASED.  It asserts the FILENAME reaches pytest, not that the
file was collected -- parsing collection out of a YAML ``run:`` block would be a
second implementation of pytest's argument handling and would break on the next
``-k`` expression.  "Was this file handed to pytest by CI at all?" is the coarse
fact that actually matters.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_EXPLORER = Path(__file__).resolve().parents[1]
_BROWSER_TESTS = _EXPLORER / "tests" / "browser"
_WORKFLOWS = _EXPLORER.parents[2] / ".github" / "workflows"

#: Files that are intentionally NOT executed by CI.
#:
#: Empty on purpose. An exemption here is a decision to stop proving something,
#: so it must be written down with a reason beside it rather than being the quiet
#: default. If a file genuinely should not run in CI, add it WITH a comment
#: saying why -- the diff then shows a human chose it.
_EXEMPT: dict[str, str] = {}

#: ``pytest ... tests/browser`` as a POSITIONAL directory argument.
#:
#: Two exclusions, each found by trying to BREAK this guard rather than by
#: reasoning about it:
#:
#:   ``(?!/)``        ``tests/browser/test_x.py`` is a file, not the directory.
#:   ``(?<![=/\w])``  ``pytest tests --ignore=tests/browser`` is the ENGINE lane
#:                    excluding this directory. Reading that as coverage
#:                    inverted the guard completely: the one invocation that
#:                    guarantees these files do not run was being counted as
#:                    proof that they do.
_BARE_DIR = re.compile(r"pytest[^\n]*(?<![=/\w])tests/browser\b(?!/)")


def _workflow_text() -> str:
    if not _WORKFLOWS.is_dir():
        pytest.skip(f"no workflow directory at {_WORKFLOWS}")
    return "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in sorted(_WORKFLOWS.glob("*.yml")))


def test_every_browser_test_file_is_executed_by_ci() -> None:
    text = _workflow_text()
    present = {p.name for p in _BROWSER_TESTS.glob("test_*.py")}
    assert present, f"no browser tests found under {_BROWSER_TESTS}"

    if _BARE_DIR.search(text):
        return                    # a directory argument covers everything in it

    missing = sorted(n for n in present if n not in _EXEMPT and n not in text)
    assert not missing, (
        "no workflow passes the tests/browser DIRECTORY to pytest any more, and "
        "these files are not named by any step either — so CI has proven nothing "
        "about them:\n"
        + "\n".join(f"    tests/browser/{n}" for n in missing)
        + "\n\nAdd them to a step in .github/workflows/browser-harness.yml (or "
          "ci.yml). If one genuinely must not run in CI, add it to _EXEMPT in "
          "this file WITH the reason.")


def test_no_exemption_is_stale() -> None:
    """An exemption naming a file that no longer exists hides the next escape."""
    present = {p.name for p in _BROWSER_TESTS.glob("test_*.py")}
    stale = sorted(set(_EXEMPT) - present)
    assert not stale, (
        f"_EXEMPT names files that no longer exist: {stale}. Remove them — a "
        "stale exemption silently covers a future file of the same name.")
