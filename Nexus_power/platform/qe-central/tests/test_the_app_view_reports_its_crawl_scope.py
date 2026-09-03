"""An app must report which mode it is in and what it is confined to.

ASKED BY A CLIENT, 2026-09-03, of a real crawl. They pointed an app at
``/web/index.php/recruitment/viewCandidates``, selected end-to-end, and asked
why the crawler explored PIM, Leave, Time, Performance, Claim and Buzz as well.

The answer is that it behaved correctly: ``e2e`` means the WHOLE application by
design — 182f39f, "E2E means the WHOLE application - lift coverage caps, keep
safety gates" — and ``target`` is the mode that confines a crawl to
``schedule.scope_paths``. Both are settable from the settings form and both are
honoured on dispatch (``routers/explorations._resolve_crawl_mode``).

But this view returned NEITHER. It surfaced ``run_environment`` out of the same
``schedule`` dict and stopped there, so nothing could read back which mode an
app was actually in. An operator who set a Target scope saw the app list
unchanged and had every reason to conclude the setting had not taken - and
nobody answering the client could point at the app and say what it was set to.

That is the failure this repository keeps re-finding under different names: the
system knew the answer and did not say it. The deploy discarded the gate's own
transcript; the login failure listed three possible causes instead of naming
the one that happened; here the mode was resolved on dispatch and never
reported.

WHY THE REAL ORM ROW AND NOT A STUB. The first draft of this file hand-rolled a
``_Row`` class and skipped on exception. Every one of its seven tests SKIPPED -
``row_to_dict`` walks ``row.__table__.columns``, which a stub does not have - so
the file passed green having never once called the function it was written to
pin. A test that cannot fail is worth less than no test, because it also
occupies the space where a real one would go. ``ClientAppRow`` needs no database
to instantiate, and using it means the columns under test are the real columns.
"""

from __future__ import annotations

import pytest

from app.db.models import ClientAppRow
from app.routers.apps import _public_view


def _view(schedule):
    """A real ORM row - unsaved, no session, no database."""
    return _public_view(ClientAppRow(
        app_id="a1", tenant_id="__platform__", name="OrangeHRM",
        base_url="https://example.invalid/web/index.php/recruitment/viewCandidates",
        schedule=schedule,
    ))


def test_the_view_actually_runs():
    """GUARD - the check the first draft of this file did not have.

    Seven assertions passed here while _public_view was never invoked. If the
    row shape drifts again this fails loudly instead of skipping into silence.
    """
    d = _view({})
    assert isinstance(d, dict) and d.get("app_id") == "a1", (
        "_public_view did not return a populated view - every assertion below "
        "is meaningless until this one holds"
    )


def test_a_target_scope_is_reported_back():
    """The client's case: the mode was set and could not be read."""
    d = _view({"crawl_mode": "target",
               "scope_paths": ["/web/index.php/recruitment"]})
    assert d["crawl_mode"] == "target"
    assert d["scope_paths"] == ["/web/index.php/recruitment"], (
        "an operator who sets a Target scope must be able to SEE it; returning "
        "nothing is why the setting looked like it had not taken"
    )


def test_end_to_end_is_reported_back():
    d = _view({"crawl_mode": "e2e"})
    assert d["crawl_mode"] == "e2e"
    assert d["scope_paths"] == [], "e2e confines nothing - say so, do not omit it"


def test_unset_is_empty_not_a_guess():
    """CONTROL - the view must not invent a mode it never resolved.

    The effective mode depends on scope_paths and on any walk plan, and is
    decided at dispatch. Reporting "explore" here would state a scope this view
    has not resolved - a confident answer that can be wrong, which is the exact
    failure mode this whole file exists to close.
    """
    d = _view({})
    assert d["crawl_mode"] == "", (
        "unset must read as unset - guessing 'explore' states a scope this view "
        "has not resolved"
    )
    assert d["scope_paths"] == []


def test_run_environment_still_works():
    """The neighbouring field this one was modelled on must not regress."""
    d = _view({"run_environment": "staging", "crawl_mode": "target",
               "scope_paths": ["/x"]})
    assert d["run_environment"] == "staging"
    assert d["crawl_mode"] == "target"


@pytest.mark.parametrize("raw,expected", [
    ([" /a ", "/b"], ["/a", "/b"]),      # whitespace trimmed
    (["", "  ", "/c"], ["/c"]),          # blanks dropped
    (None, []),                          # absent
    ("/not-a-list", []),                 # a bare string must not become chars
])
def test_scope_paths_are_cleaned(raw, expected):
    assert _view({"scope_paths": raw})["scope_paths"] == expected
