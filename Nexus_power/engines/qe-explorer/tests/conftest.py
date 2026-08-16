"""Make the qe-explorer service root importable as the ``app`` package.

The pure-logic units under test (``app.inventory`` / ``app.fingerprint``) have
NO browser or DB dependency, so the suite runs anywhere Python does.  We add the
service root (parent of ``app/``) to ``sys.path`` rather than requiring an
installed package.
"""
from __future__ import annotations

import os
import sys

_SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE_ROOT not in sys.path:
    sys.path.insert(0, _SERVICE_ROOT)

# M0.5 T-SEC-01 — ``QEC_EXPLORER_TOKEN`` no longer has a shipped default (a
# development secret baked into the image is a credential anyone with this repo
# holds).  Empty now means fail-closed: nothing signs and nothing authenticates.
# The suite therefore has to state its own deterministic test secret, exactly as
# qe-central's conftest states ``NEXUS_JWT_SECRET`` — set BEFORE ``app.config``
# is imported anywhere, because the settings singleton reads the environment at
# import time.
os.environ.setdefault("NEXUS_ENV", "test")
os.environ.setdefault("QEC_EXPLORER_TOKEN", "unit-test-explorer-token")
