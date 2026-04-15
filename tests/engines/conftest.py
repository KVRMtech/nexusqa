"""
Engine test isolation — ensures each test file imports from its own engine.

Problem:  All 10 engine directories are on sys.path (added by root conftest).
          They all contain a file called ``main.py`` AND an ``app/`` sub-package,
          so Python's module cache returns whichever was imported first, breaking
          every other engine's tests.

Solution: An autouse fixture that, before *every* test function:
  1. Removes cached ``main`` and ``app`` modules from ``sys.modules``
  2. Temporarily reorders ``sys.path`` so only the owning engine dir is first
"""

import importlib
import os
import sys

import pytest

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# All engine directories (must match root conftest ENGINE_DIRS order)
_ENGINE_NAMES = [
    "shield-engine", "ears-engine", "eyes-engine", "heart-engine",
    "backbone-engine", "nerves-engine", "legs-engine", "hands-engine",
    "spine-engine", "mouth-engine",
]
_ENGINE_DIRS = [os.path.join(_PROJECT_ROOT, "engines", d) for d in _ENGINE_NAMES]

# Map test file prefix → engine directory
# Uses prefix matching so test_ears.py AND test_ears_modules.py both map to ears-engine
_TEST_PREFIX_MAP = {
    "test_backbone": os.path.join(_PROJECT_ROOT, "engines", "backbone-engine"),
    "test_ears":     os.path.join(_PROJECT_ROOT, "engines", "ears-engine"),
    "test_eyes":     os.path.join(_PROJECT_ROOT, "engines", "eyes-engine"),
    "test_hands":    os.path.join(_PROJECT_ROOT, "engines", "hands-engine"),
    "test_heart":    os.path.join(_PROJECT_ROOT, "engines", "heart-engine"),
    "test_legs":     os.path.join(_PROJECT_ROOT, "engines", "legs-engine"),
    "test_mouth":    os.path.join(_PROJECT_ROOT, "engines", "mouth-engine"),
    "test_nerves":   os.path.join(_PROJECT_ROOT, "engines", "nerves-engine"),
    "test_shield":   os.path.join(_PROJECT_ROOT, "engines", "shield-engine"),
    "test_spine":    os.path.join(_PROJECT_ROOT, "engines", "spine-engine"),
}


def _resolve_engine_dir(test_stem: str):
    """Resolve engine directory from test file stem using prefix matching."""
    # Try exact match first
    if test_stem in _TEST_PREFIX_MAP:
        return _TEST_PREFIX_MAP[test_stem]
    # Try prefix match (e.g. test_ears_modules → test_ears → ears-engine)
    for prefix, engine_dir in sorted(_TEST_PREFIX_MAP.items(), key=lambda x: -len(x[0])):
        if test_stem.startswith(prefix):
            return engine_dir
    return None


def _clean_engine_modules():
    """Remove cached 'main' and 'app' modules to prevent cross-contamination."""
    to_remove = [
        key for key in list(sys.modules.keys())
        if key in ("main", "app") or key.startswith("app.")
    ]
    for key in to_remove:
        del sys.modules[key]


@pytest.fixture(autouse=True)
def _isolate_engine_main(request):
    """Ensure ``from main import …`` and ``from app.* import …`` resolve to the correct engine."""
    test_stem = os.path.splitext(os.path.basename(str(request.fspath)))[0]
    engine_dir = _resolve_engine_dir(test_stem)

    if engine_dir is None:
        # Not an engine test file — leave sys.path alone
        yield
        return

    # ── Setup ──────────────────────────────────────────────
    saved_path = sys.path[:]
    saved_main = sys.modules.pop("main", None)

    # Clean all engine modules (app, main, app.*)
    _clean_engine_modules()

    # Remove *all* engine dirs, then put only the correct one at front
    clean_path = [p for p in sys.path if p not in _ENGINE_DIRS]
    sys.path = [engine_dir] + clean_path

    yield

    # ── Teardown ───────────────────────────────────────────
    _clean_engine_modules()
    sys.path = saved_path
    if saved_main is not None:
        sys.modules["main"] = saved_main
