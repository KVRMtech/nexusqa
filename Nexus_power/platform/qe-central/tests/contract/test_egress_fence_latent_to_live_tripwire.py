"""The cross-tenant egress fence is latent ONLY because of a schema default.

THE DEFECT ITSELF IS RECORDED ELSEWHERE, DELIBERATELY.
``tests/fleet/test_t_fl_08_concurrency_redteam.py::
test_the_egress_fence_survives_concurrent_dispatch_on_one_worker`` asserts the
clobber deterministically under ``xfail(strict=True, raises=AssertionError)``.
That is the defect record and it lives in the file that owns the behaviour. This
file does NOT restate it — two records of one defect drift apart, and the one
nobody updated is the one the next reader finds.

WHAT IS NOT COVERED THERE, AND IS COVERED HERE
==============================================
That record says "latent at capacity=1, live above it" in PROSE. Prose does not
fail a build. Nothing in the schema, the registry API or the scheduler refuses a
larger capacity — it is ordinary, documented configuration — so the entire reason
this cross-tenant hole is not an incident today is one ``server_default`` in
``qec_022``.

A one-line configuration change turns a latent defect into a live cross-tenant
egress leak, and nothing anywhere would say so. This is the tripwire for exactly
that transition: it fails at the commit that raises the default while the fence
is still shared, rather than in an incident afterwards.

WHY A SEPARATE FILE RATHER THAN ANOTHER ASSERTION IN THAT ONE
=============================================================
Different trigger, different lifetime. The defect record is deleted the day the
fence becomes per-crawl. This tripwire is about the CONFIGURATION that makes the
defect reachable, and it must keep working during the window between "someone
raises capacity" and "someone fixes the fence" — which is precisely the window in
which the leak would go live unnoticed.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.routers.explorations import _write_egress_allowlist

#: Located by GLOB, not by exact filename: a migration renamed on the way in
#: would otherwise leave this tripwire quietly unable to find what it guards,
#: and a tripwire that cannot see its subject is worse than none.
_VERSIONS_DIR = Path(__file__).resolve().parents[2] / "alembic_qec" / "versions"


def test_capacity_still_defaults_to_one_while_the_fence_is_shared():
    """Fail the build when the egress leak stops being latent.

    Two conditions make the leak live: a fence shared between concurrent crawls
    on one worker, AND a capacity that admits more than one. Both are checked
    here, because the assertion is only meaningful while the first still holds.
    """
    # ── Is the fence still shared? The writer takes no crawl identifier, so it
    #    CANNOT be per-crawl — that signature is the defect in one line.
    params = list(inspect.signature(_write_egress_allowlist).parameters)
    if any("crawl" in p or "exploration" in p for p in params):
        pytest.skip(
            "the fence writer now takes a per-crawl identifier, so the shared "
            "fence may be repaired and this tripwire no longer describes "
            "reality - re-read it against the new design before trusting it")

    # ── Can it be found at all? A tripwire whose silence might mean "I could
    #    not look" is not a tripwire.
    candidates = sorted(_VERSIONS_DIR.glob("qec_022*.py"))
    assert candidates, (
        f"no qec_022* migration under {_VERSIONS_DIR}: this tripwire cannot see "
        f"the schema default it guards, so its silence would mean nothing")

    found = []
    for migration in candidates:
        for m in re.finditer(r'"capacity"[^)]*?server_default="(\d+)"',
                             migration.read_text(encoding="utf-8")):
            found.append((migration.name, int(m.group(1))))
    assert found, (
        f"found {[c.name for c in candidates]} but no capacity server_default "
        f"in any of them - the tripwire cannot see what it guards")

    live = [(name, value) for name, value in found if value != 1]
    assert not live, (
        f"CAPACITY DEFAULT RAISED WHILE THE EGRESS FENCE IS STILL SHARED: "
        f"{live}. The fence is one file per WORKER "
        f"(_write_egress_allowlist{tuple(params)} takes no crawl id), and "
        f"concurrent dispatches to one worker overwrite each other's live "
        f"fence. At capacity 1 that window never opens; above 1 it is a "
        f"cross-tenant egress leak by default. Repair the fence (per-crawl "
        f"files, or a per-worker dispatch lock) BEFORE raising this default. "
        f"See tests/fleet/test_t_fl_08_concurrency_redteam.py::"
        f"test_the_egress_fence_survives_concurrent_dispatch_on_one_worker.")
