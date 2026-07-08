"""Test bootstrap for the QE-Central unit suite.

Puts the service root (and the SDK checkout, for envelope imports) on
``sys.path`` and pins deterministic env values BEFORE ``app.config`` is
imported anywhere — the ``settings`` singleton reads the environment at
import time.
"""
from __future__ import annotations

import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SERVICE_ROOT = os.path.abspath(os.path.join(_TESTS_DIR, ".."))
_SDK_PATH = os.path.abspath(
    os.path.join(_SERVICE_ROOT, "..", "..", "sdk", "nexus-sdk")
)
for _p in (_SERVICE_ROOT, _SDK_PATH):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Deterministic config for the singleton (set BEFORE any app import).
os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("NEXUS_JWT_SECRET", "unit-test-secret-qe-central")
os.environ.setdefault("QEC_LOG_LEVEL", "WARNING")
