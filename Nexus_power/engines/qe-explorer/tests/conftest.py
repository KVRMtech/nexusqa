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
