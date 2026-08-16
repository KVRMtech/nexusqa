"""The crawler subsystem's source text, as one blob (M0.3).

Lives in its own module rather than in ``conftest`` because the test tree
now contains more than one ``conftest.py`` (``tests/security/conftest.py``,
``tests/browser/``, ``tests/characterization/``). pytest imports each of them
as a top-level module literally named ``conftest``, so a bare
``from conftest import crawler_subsystem_source`` binds to whichever one landed
in ``sys.modules`` first — in practice ``tests/security/conftest.py``, which
killed three test modules at import with

    ImportError: cannot import name 'crawler_subsystem_source' from 'conftest'

This name is unambiguous and cannot be shadowed that way.

Several tests assert on the crawler's SOURCE TEXT — that a bound exists, that a
log line carries its evidence, that a constant has a given value. M0.3 split
that source across cohesive modules, so "the crawler's source" is no longer one
file; this returns the subsystem's text as one blob, which is what those tests
always meant: the assertions are about the crawler SUBSYSTEM, not about which
file a line happens to sit in today.
"""
from __future__ import annotations

import os

#: Service root — the parent of ``app/`` (…/engines/qe-explorer).
SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Backwards-compatible alias for the private name this module used to export.
_SERVICE_ROOT = SERVICE_ROOT

#: The modules M0.3 split the crawler across. A name that does not exist yet is
#: skipped, so this list can lead a refactor rather than trail it.
SUBSYSTEM_MODULES = (
    "crawler.py", "crawl_constants.py", "guard_context.py", "budget.py",
    "frontier.py", "state_identity.py", "coverage.py", "emitter.py",
    "filler.py", "oracle_gateway.py", "auth_flow.py", "submit.py",
    "discovery.py", "walker.py",
)

#: Legacy private alias (some callers imported it under this name).
_SUBSYSTEM_MODULES = SUBSYSTEM_MODULES


def crawler_subsystem_source() -> str:
    """The crawler subsystem's source as a single string."""
    app_dir = os.path.join(SERVICE_ROOT, "app")
    parts = []
    for name in SUBSYSTEM_MODULES:
        path = os.path.join(app_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                parts.append(handle.read())
    return chr(10).join(parts)
