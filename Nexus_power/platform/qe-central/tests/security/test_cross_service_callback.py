"""CROSS-SERVICE INTEROP — the explorer signs, qe-central verifies.

Why this exists as a test rather than as an assumption.
======================================================
qe-central and qe-explorer are separate containers with separate dependency
sets, and BOTH publish a package called ``app`` — so they cannot be imported
into one interpreter.  Every other test in this suite therefore exercises one
side or the other.  That leaves the most expensive possible failure untested:
the two halves agreeing about the SHAPE of the signature but disagreeing about
its BYTES, which would reject every genuine callback in production while every
unit test stayed green.

So this runs the real signing code in a subprocess rooted at the explorer, and
the real verification code in a subprocess rooted at qe-central, over the same
shared secret — the deployed handshake, end to end.

It also proves the FOUR properties on the wire rather than in a helper: accepted
once, replay refused, re-scoped envelope refused, tampered body refused.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[4]
EXPLORER = ROOT / "engines/qe-explorer"
CENTRAL = ROOT / "platform/qe-central"
SDK = ROOT / "sdk/nexus-sdk"

TOKEN = "cross-service-fleet-token-2f9c41ab"
CRAWL = "a" * 32
TENANT = "t1"
BODY = json.dumps({"crawl_id": CRAWL, "tenant_id": "t1", "stop_reason": "completed"},
                  sort_keys=True, separators=(",", ":")).encode("utf-8")


def _run(script: str) -> str:
    import os

    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=180,
        env={**os.environ, "QEC_EXPLORER_TOKEN": TOKEN, "NEXUS_ENV": "test"},
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return proc.stdout


@pytest.fixture(scope="module")
def explorer_signature() -> str:
    """A signature produced by the EXPLORER's own code, in its own interpreter."""
    out = _run(f'''
import sys
sys.path.insert(0, r"{EXPLORER}")
from app.config import Settings
from app.hmac_auth import tenant_scope
s = Settings(explorer_token="{TOKEN}")
print(s.sign_payload({BODY!r}, scope=tenant_scope("complete", "{TENANT}", "{CRAWL}")))
''')
    return out.strip()


@pytest.fixture(scope="module")
def central_verdicts(explorer_signature) -> dict:
    """qe-central's verdicts on that signature, in ITS own interpreter."""
    out = _run(f'''
import sys
sys.path.insert(0, r"{CENTRAL}")
sys.path.insert(0, r"{SDK}")
from app.clients.config import Phase1Settings
from app.security.hmac_auth import tenant_scope
s = Phase1Settings(explorer_token="{TOKEN}")
sig = {explorer_signature!r}
scope = tenant_scope("complete", "{TENANT}", "{CRAWL}")
print("accepted_once=%s" % s.verify_signature({BODY!r}, sig, scope=scope))
print("replay_refused=%s" % (not s.verify_signature({BODY!r}, sig, scope=scope)))
print("rescope_refused=%s" % (not s.verify_signature({BODY!r}, sig, scope=tenant_scope("complete", "t2", "{CRAWL}"))))
print("tamper_refused=%s" % (not s.verify_signature({BODY + b" "!r}, sig, scope=scope)))
print("wrong_key_refused=%s" % (
    not Phase1Settings(explorer_token="a-different-fleet-secret").verify_signature(
        {BODY!r}, sig, scope=scope)))
''')
    return dict(
        (line.split("=", 1)[0], line.split("=", 1)[1] == "True")
        for line in out.strip().splitlines() if "=" in line
    )


def test_the_explorer_produces_a_v2_envelope(explorer_signature):
    assert explorer_signature.startswith("v2;kid=")
    assert ";nonce=" in explorer_signature and ";sig=" in explorer_signature


@pytest.mark.parametrize("case", [
    "accepted_once",       # POSITIVE: the real handshake works
    "replay_refused",      # R7
    "rescope_refused",     # T-SEC-06 scope binding
    "tamper_refused",      # integrity
    "wrong_key_refused",   # T-SEC-11 key identity
])
def test_cross_service_callback_properties(central_verdicts, case):
    assert central_verdicts.get(case) is True, f"{case} did not hold on the wire"
