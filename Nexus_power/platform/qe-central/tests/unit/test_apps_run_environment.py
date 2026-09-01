"""Multi-env selector validation: an app's run_environment must name an existing
Environment Profile (else the daemon would fail-close every cycle). Empty clears it."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.routers.apps import _validated_run_environment


class _Result:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _Session:
    """Fake async session: execute() resolves to a fixed scalar (env id or None)."""
    def __init__(self, val):
        self._val = val

    async def execute(self, *a, **k):
        return _Result(self._val)


def _run(val, name):
    return asyncio.run(_validated_run_environment(_Session(val), "t1", "a1", name))


def test_empty_clears_the_selector():
    assert _run(None, "") == ""
    assert _run(None, "   ") == ""
    assert _run(None, None) == ""


def test_existing_profile_is_accepted():
    assert _run("env_abc", "uat") == "uat"
    assert _run("env_abc", "  uat  ") == "uat"   # trimmed


def test_unknown_profile_is_422():
    with pytest.raises(HTTPException) as ei:
        _run(None, "nope")
    assert ei.value.status_code == 422
    assert "does not match" in str(ei.value.detail)
