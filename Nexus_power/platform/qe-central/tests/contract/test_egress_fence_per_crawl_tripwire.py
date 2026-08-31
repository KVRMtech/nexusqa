"""Capacity is unclamped ONLY while the CONSUMER selects the fence per crawl.

THE TRANSITION THIS GUARDS — the mirror image of the old latent-to-live
tripwire this file replaces (``test_egress_fence_latent_to_live_tripwire.py``,
deleted in the same change that repaired the fence). That file guarded the
window between "someone raises capacity" and "someone fixes the fence". The
fence is fixed; the new window is between "someone breaks the fence" and
"someone notices": ``FENCE_IS_PER_WORKER`` is False, so the scheduler grants
real concurrency per worker — which is safe precisely BECAUSE:

  1. the PRODUCER writes one fence file per crawl (the writer takes a crawl
     id — pinned here by signature, as before);
  2. the CONSUMER — the shipped ``engines/qe-explorer/squid.conf`` — requires
     a proxy login on every request and applies each generated per-crawl ACL
     to exactly the crawl that authenticated (the ARB record's point: the
     consumer is where the old design was broken, so the consumer is what
     this tripwire reads).

Either half regressing while the clamp stays off re-opens the T-FL-08
cross-tenant egress leak at any capacity > 1 — and, as before, nothing else
anywhere would say so. Asserted as BYTES against the repository's squid.conf
(the file compose bind-mounts into the proxy), with the expectations frozen in
``contracts/fleet_egress_fence_v1.json`` so this suite and the explorer's
assert the same truth without sharing an interpreter.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from app.controlplane.scheduling import worker_registry as wr
from app.routers.explorations import _write_egress_allowlist


def _repo_file(*parts: str) -> Path:
    """Walk up to the Nexus_power root (the suite is collected from the repo
    root in CI and the service root locally; a relative literal resolves in
    only one of them)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent.joinpath(*parts)
        if candidate.is_file():
            return candidate
    raise AssertionError(
        f"{'/'.join(parts)} not found above {here} — this tripwire cannot see "
        "what it guards, and a tripwire whose silence might mean 'I could not "
        "look' is worse than none")


CONTRACT = json.loads(
    _repo_file("contracts", "fleet_egress_fence_v1.json").read_text(
        encoding="utf-8"))


def _unclamped() -> bool:
    return wr.FENCE_IS_PER_WORKER is False


def test_the_producer_still_writes_per_crawl_fences():
    params = list(inspect.signature(_write_egress_allowlist).parameters)
    writer_per_crawl = any("crawl" in p or "exploration" in p for p in params)
    if not _unclamped():
        pytest.skip("FENCE_IS_PER_WORKER is True — the admission clamp is the "
                    "guard again; the pairing test owns that direction")
    assert writer_per_crawl, (
        "CAPACITY IS UNCLAMPED BUT THE FENCE WRITER TAKES NO CRAWL ID: "
        f"_write_egress_allowlist{tuple(params)}. Two concurrent crawls on one "
        "worker share one fence file again and the last writer re-fences the "
        "other tenant's live browser (T-FL-08). Restore the per-crawl writer "
        "or flip FENCE_IS_PER_WORKER to True.")


def test_the_shipped_squid_conf_still_selects_the_fence_per_crawl():
    """The consumer, as bytes. squid enforces exactly what this file says —
    a drifted conf IS a drifted fence, whatever the python looks like."""
    if not _unclamped():
        pytest.skip("FENCE_IS_PER_WORKER is True — clamp guards; see pairing test")
    conf = _repo_file("engines", "qe-explorer", "squid.conf").read_text(
        encoding="utf-8")
    for needle in CONTRACT["squid_conf_must_contain"]:
        assert needle in conf, (
            f"squid.conf lost {needle!r} while capacity is unclamped — the "
            "proxy would stop selecting fences per crawl and every "
            "capacity>1 worker re-opens the cross-tenant egress leak. Either "
            "restore the per-crawl mechanism (contracts/"
            "fleet_egress_fence_v1.json) or flip FENCE_IS_PER_WORKER to True.")
    for needle in CONTRACT["squid_conf_must_not_contain"]:
        assert needle not in conf, (
            f"squid.conf regained {needle!r} — the legacy shared per-worker "
            "allow rule fences every crawl on the worker with one file again.")


def test_the_allow_rules_sit_between_the_challenge_and_the_final_deny():
    """Order is enforcement in squid: challenge (407) BEFORE the per-crawl
    allows, deny-all AFTER them. An include moved below deny-all allows
    nothing; a challenge moved below the include never authenticates."""
    if not _unclamped():
        pytest.skip("FENCE_IS_PER_WORKER is True — clamp guards; see pairing test")
    conf = _repo_file("engines", "qe-explorer", "squid.conf").read_text(
        encoding="utf-8")
    challenge = conf.index("http_access deny !fenced_crawl")
    include = conf.index("include /etc/squid/allowlist/crawls.conf")
    deny_all = conf.index("http_access deny all")
    assert challenge < include < deny_all, (
        "squid.conf's http_access ordering changed — the per-crawl fence "
        "selection no longer does what the contract froze")
