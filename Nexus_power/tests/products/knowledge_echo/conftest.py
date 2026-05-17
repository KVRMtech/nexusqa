"""Path setup for echo product tests.

Both ``engines/knowledge-fusion-engine`` and ``products/knowledge-echo``
expose a top-level ``app`` package; this conftest purges any cached
``app*`` modules and prepends the echo directory so the echo's ``app``
wins resolution during this collection scope.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ECHO_DIR = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "products", "knowledge-echo")
)


def _purge_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)


_purge_app_modules()
if _ECHO_DIR in sys.path:
    sys.path.remove(_ECHO_DIR)
sys.path.insert(0, _ECHO_DIR)


def pytest_collectstart(collector):  # noqa: D401
    """Re-establish path priority on every test module collection."""
    _purge_app_modules()
    if _ECHO_DIR in sys.path:
        sys.path.remove(_ECHO_DIR)
    sys.path.insert(0, _ECHO_DIR)
