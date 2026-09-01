"""A login proof must not depend on how chatty the run was.

THE DEFECT. The runner returned ``out.slice(-8000)`` — the LAST 8000 characters of
a run's output. The ``[nexus-auth]`` markers that prove a login happened are printed
at the START of a run, before any test output, so a tail window drops them the
moment a run gets talkative. ``login_probe`` then saw no marker, returned
UNREADABLE, and the per-card **Verify** button could never prove anything: measured
live at ``output_len=8000 auth_lines=[]``.

The preflight survived only because it happens to run with video and screenshots
off, so it stayed under the cap. That is proof by luck. The two paths build
IDENTICAL bundles and read the output IDENTICALLY — the only difference was how
much unrelated text the run happened to print.

Raising the cap would not fix it (a bigger suite blows through any cap) and neither
would turning video off in the verify path (same latent bug, deferred). The marker
itself must survive.
"""
import re
import subprocess
from pathlib import Path

_SERVER = Path("../../infrastructure/runner/server.js").resolve()
_SRC = _SERVER.read_text(encoding="utf-8")


def _clip(text: str) -> str:
    """Run the REAL clipOutput from the shipped runner — not a Python re-implementation,
    which would happily pass while the deployed JS stayed broken."""
    body = re.search(r"const OUT_TAIL[\s\S]*?\n}\n", _SRC).group(0)
    js = body + "\nprocess.stdout.write(clipOutput(JSON.parse(process.argv[1])));"
    import json
    r = subprocess.run(["node", "-e", js, json.dumps(text)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


MARK = "[nexus-auth] recipe login OK (3 steps, home reached)"


def test_the_proof_marker_survives_a_chatty_run():
    """THE REGRESSION. Marker first, then far more output than the window."""
    out = _clip(MARK + "\n" + ("x" * 20000))
    assert MARK in out


def test_the_tail_is_still_returned():
    """Truncation exists for a reason — the end of the run is what diagnoses a
    failure. Keeping the marker must not cost the tail."""
    out = _clip(MARK + "\n" + ("x" * 20000) + "\nFAILED somewhere at the end")
    assert "FAILED somewhere at the end" in out


def test_short_output_is_returned_untouched():
    assert _clip("hello world") == "hello world"


def test_a_run_with_no_marker_is_an_ordinary_tail():
    """No marker to save = the old behaviour exactly, so nothing else changes."""
    out = _clip("y" * 20000)
    assert len(out) == 8000 and set(out) == {"y"}


def test_every_marker_line_is_kept_not_just_the_first():
    a, b = "[nexus-auth] form login OK", "[nexus-auth] recipe login OK (2 steps)"
    out = _clip(a + "\n" + b + "\n" + ("z" * 20000))
    assert a in out and b in out


def test_a_marker_already_inside_the_tail_is_not_duplicated():
    out = _clip(("z" * 20000) + "\n" + MARK)
    assert out.count(MARK) == 1


def test_the_reader_can_actually_read_what_the_runner_now_returns():
    """End to end: the shipped clip feeding the shipped probe. Either half alone
    can pass while the pair stays broken."""
    from app.services.test_factory import login_probe
    out = _clip(MARK + "\n" + ("x" * 20000))
    v = login_probe.read_outcome(out)
    assert v["outcome"] == login_probe.PROVEN
    assert v["proven"] is True


def test_the_old_tail_only_window_would_have_failed_this():
    """Pins WHY the fix is needed — if this ever passes, the marker is no longer
    at the head and the guard above is testing nothing."""
    from app.services.test_factory import login_probe
    tail_only = (MARK + "\n" + ("x" * 20000))[-8000:]
    assert login_probe.read_outcome(tail_only)["outcome"] == login_probe.UNREADABLE


# ── the probe now says WHY it could not read an outcome ──────────────────────

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()


def test_an_unreadable_probe_now_reports_why():
    """UNREADABLE cannot distinguish 'the runner produced nothing' from 'the login
    never ran' from 'our markers changed'. Without this the defect was invisible."""
    assert "def _probe_diag(" in _ROUTER
    assert "output_len=%d" in _ROUTER
    assert _ROUTER.count("_probe_diag(") == 3   # definition + both call sites


def test_the_diagnostic_logs_our_own_markers_only():
    """It must never log page content — that is where a credential or customer data
    would be."""
    seg = _ROUTER[_ROUTER.index("def _probe_diag("):]
    seg = seg[:seg.index("async def ")]
    assert '"[nexus-auth]" in ln' in seg
    assert "output[" not in seg, "no raw slice of the run's output may be logged"
