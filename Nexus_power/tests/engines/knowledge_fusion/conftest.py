"""Path setup for fusion-engine tests.

Both ``engines/knowledge-fusion-engine`` and ``products/knowledge-echo``
expose a top-level ``app`` package, which would collide if both lived
in ``sys.modules`` at once. We purge any pre-existing ``app*`` modules
before adding the fusion engine to ``sys.path`` so each test module
sees a fresh, fusion-scoped ``app`` namespace.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUSION_DIR = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "engines", "knowledge-fusion-engine")
)


def _purge_app_modules() -> None:
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            sys.modules.pop(name, None)


_purge_app_modules()
# Ensure fusion engine's directory is first on sys.path so its ``app``
# package wins resolution over any other package of the same name.
if _FUSION_DIR in sys.path:
    sys.path.remove(_FUSION_DIR)
sys.path.insert(0, _FUSION_DIR)


def pytest_collectstart(collector):  # noqa: D401
    """Re-establish path priority on every test module collection."""
    _purge_app_modules()
    if _FUSION_DIR in sys.path:
        sys.path.remove(_FUSION_DIR)
    sys.path.insert(0, _FUSION_DIR)
