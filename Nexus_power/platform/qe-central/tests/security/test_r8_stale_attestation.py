"""R8 — STALE ATTESTATION (an expired disposable-env attestation still submits).

ATTACK
======
``Attestation.is_submit_capable(now_ms)`` compared ``now_ms`` against
``expires_at_ms``.  ``expires_at_ms`` is EPOCH millis (qe-central converts the
stored ISO ``expires_at`` at dispatch), but the only caller passed
``crawler.now_ms()`` — the crawl's MONOTONIC clock, i.e. milliseconds since the
crawl started, a number in the thousands.

``5_000 < 1_760_000_000_000`` is true, and stays true for about fifty thousand
years.  The freshness gate could not expire anything: an attestation that lapsed
months ago authorised an irreversible submit.

EXPECTED
========
A lapsed attestation is refused.  A fresh one is accepted.  And a monotonic
reading is REFUSED rather than compared, so the two clock domains cannot meet
again (SI-08).
"""
from __future__ import annotations

import pathlib
import re

import pytest

EXPLORER = pathlib.Path(__file__).resolve().parents[4] / "engines/qe-explorer"


@pytest.fixture(scope="module")
def guard_src() -> str:
    return (EXPLORER / "app/guard.py").read_text(encoding="utf-8")


# ── the structural half: the two clocks cannot meet ────────────────────────

def test_r8_freshness_no_longer_takes_the_crawls_monotonic_clock(guard_src):
    """The call site is the defect.  ``classify_request`` still receives
    ``now_ms`` (correctly, for the auth/submit WINDOWS, which measure duration)
    — but the ATTESTATION check must not be handed it."""
    assert "attestation.is_submit_capable(now_ms)" not in guard_src
    assert "attestation.is_submit_capable()" in guard_src


def test_r8_the_parameter_is_named_for_its_clock_domain(guard_src):
    """``now_ms`` was ambiguous enough to cause this.  ``now_epoch_ms`` is not."""
    assert "def is_submit_capable(self, now_epoch_ms: int | None = None)" in guard_src


def test_r8_a_monotonic_reading_is_refused_not_compared(guard_src):
    assert "_MIN_PLAUSIBLE_EPOCH_MS" in guard_src
    assert "attestation_clock_domain_error" in guard_src


def test_r8_qe_central_emits_the_deadline_in_the_same_domain():
    """One canonical representation for a persisted/protocol timestamp: the ISO
    ``expires_at`` on the row is converted to epoch millis at dispatch."""
    from app.routers.explorations import _explorer_attestation

    out = _explorer_attestation({
        "attested_by": "qa@client.example", "env_kind": "disposable",
        "expires_at": "2031-01-01T00:00:00Z",
    })
    assert out["expires_at_ms"] == 1_924_992_000_000


# ── the behavioural half, run in the explorer's own interpreter ────────────
#
# qe-explorer and qe-central both publish a package called ``app``, so the two
# cannot be imported into one process.  The attack is therefore executed in a
# SUBPROCESS rooted at the explorer — a real run of the real gate, not a mock.

_SCRIPT = r"""
import sys, time
sys.path.insert(0, r"{root}")
from app.guard import Attestation

now = int(time.time() * 1000)
fresh   = Attestation(attested_by="qa", env_kind="disposable",
                      expires_at_ms=now + 600_000)
lapsed  = Attestation(attested_by="qa", env_kind="disposable",
                      expires_at_ms=now - 1)
results = {{
    "fresh_accepted":        fresh.is_submit_capable() is True,
    "lapsed_refused":        lapsed.is_submit_capable() is False,
    "monotonic_refused":     all(
        fresh.is_submit_capable(m) is False for m in (0, 1_000, 5_000, 1_800_000)),
    "explicit_epoch_works":  fresh.is_submit_capable(now) is True,
    "past_epoch_expires":    fresh.is_submit_capable(now + 600_001) is False,
    "boundary_is_exclusive": Attestation(
        attested_by="qa", env_kind="disposable",
        expires_at_ms=now).is_submit_capable(now) is False,
    "prod_never_capable":    Attestation(
        attested_by="qa", env_kind="prod",
        expires_at_ms=now + 600_000).is_submit_capable() is False,
    "unattributed_refused":  Attestation(
        attested_by="", env_kind="disposable",
        expires_at_ms=now + 600_000).is_submit_capable() is False,
    "no_deadline_refused":   Attestation(
        attested_by="qa", env_kind="disposable",
        expires_at_ms=None).is_submit_capable() is False,
}}
for key, ok in sorted(results.items()):
    print(f"{{key}}={{ok}}")
"""


@pytest.fixture(scope="module")
def explorer_results() -> dict:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT.format(root=str(EXPLORER))],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return dict(
        (m.group(1), m.group(2) == "True")
        for m in re.finditer(r"^(\w+)=(True|False)$", proc.stdout, re.M)
    )


@pytest.mark.parametrize("case", [
    "fresh_accepted",           # positive: a live attestation still submits
    "lapsed_refused",           # THE attack: an expired one does not
    "monotonic_refused",        # SI-08: the clock domains cannot meet
    "explicit_epoch_works",
    "past_epoch_expires",       # advance time past the window ⇒ refused
    "boundary_is_exclusive",    # exactly-at-expiry is expired
    "prod_never_capable",
    "unattributed_refused",
    "no_deadline_refused",
])
def test_r8_attestation_freshness(explorer_results, case):
    assert explorer_results.get(case) is True, f"{case} did not hold"
