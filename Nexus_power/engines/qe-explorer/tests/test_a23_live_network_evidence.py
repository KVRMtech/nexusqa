"""GATE 3 / A23 — network evidence, asserted against a REAL LIVE APPLICATION.

    real deployment  →  real crawl  →  endpoint map  →  action joins  →  asserted

WHY THIS FILE AND NOT THE M2.5 FIXTURE GATE
===========================================
M2.5 proves the MECHANISM against fixture ``30-network-retry-poll-ratelimit``,
built to exhibit a scripted 503,503,200 retry, a four-iteration poll, a 429
backoff and a 500. That is the right way to prove a mechanism and the wrong way
to prove it survives contact with a real application, because a fixture cannot
surprise you — and, as this module's own regressions record, cannot even reach
the code path a stored manifest takes.

``measure_network_evidence.py`` already crawls a live deployment, but it asserts
NOTHING by design: it is an instrument that prints what came back. An instrument
nobody runs proves nothing next month. This file is the gate over the evidence
that instrument recorded, so the properties A23 requires are checked on every
push instead of the day someone remembers to look.

WHAT THE EVIDENCE IS
====================
``Nexus_power/evidence/a23_live_network/manifest.jsonl`` — the manifest a real
crawl of **https://vkpowerlife.136-85-106-73.sslip.io/** wrote: a deployed
Next.js application on a real VM, over real HTTPS, reached over the public
internet. Not a proving ground, not a fixture, not localhost. 68 network events
across 7 endpoints, recorded through the production ``Crawler`` and
``PlaywrightBrowserPort`` with **no boundary approvals and no walk attestation** —
a read-only posture, because it is somebody's live deployment.

Re-record it with::

    cd engines/qe-explorer
    QEC_MEASURE_OUT=$PWD/_a23_live QEC_MEASURE_USER=25000001 \
    QEC_MEASURE_PASSWORD=... QEC_MEASURE_OTP=... \
      python measure_network_evidence.py https://vkpowerlife.136-85-106-73.sslip.io/

WHAT THIS APPLICATION CANNOT PROVE, STATED RATHER THAN WORKED AROUND
====================================================================
Every one of the 68 requests is a GET that returned 200, because the app is a
static export behind a catch-all — a direct probe confirmed even a nonexistent
route answers 200. So on THIS application:

  * there is no auth pattern to observe (``auth_pattern: none`` on all 68);
  * there are no request bodies, so body-shape capture is unexercised;
  * the 5xx oracle correctly stays SILENT, which is a real no-false-positive
    result over 68 events of genuine traffic and is NOT evidence that it fires.

Those axes are proven on the M2.5 fixture and on the frozen-data contract test,
and they are named here so this file's green cannot be read as covering them.
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Mapping

import pytest

from app import endpoint_inventory as inv

SERVICE_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = SERVICE_ROOT.parent.parent / "evidence" / "a23_live_network"
MANIFEST = EVIDENCE / "manifest.jsonl"
STAMP = EVIDENCE / "stamp.json"


def _records() -> list[dict[str, Any]]:
    assert MANIFEST.is_file(), (
        f"the A23 live network evidence is missing from {MANIFEST}.\n"
        f"Re-record it with measure_network_evidence.py — see this module's "
        f"docstring. There is deliberately NO fixture fallback: A23's whole "
        f"claim is that the joins hold on a real application's traffic.")
    return [json.loads(line) for line in
            MANIFEST.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture(scope="module")
def evidence() -> dict[str, Any]:
    records = _records()
    meta = next((r for r in records
                 if (r.get("type") or r.get("kind")) == "crawl_meta"), {})
    events = [dict(c) for r in records for c in (r.get("network_calls") or [])]
    return {
        "records": records,
        "meta": meta,
        "events": events,
        "stamp": json.loads(STAMP.read_text(encoding="utf-8")),
        "inventory": inv.build_inventory(events),
    }


# ── 1. IS IT REAL? ───────────────────────────────────────────────────────────

def test_the_evidence_is_a_live_deployment_not_a_fixture(evidence) -> None:
    """The first evidence question, asserted rather than asserted-about.

    A crawl of 127.0.0.1 is a fixture crawl however good it is, and Gate 3 does
    not accept one here. The target has to be a real host over TLS.
    """
    target = str(evidence["meta"].get("target_url") or "")
    assert target.startswith("https://"), (
        f"the recorded target is {target!r} — A23 requires a live application "
        f"over real HTTPS, not a local fixture server")
    host = target.split("//", 1)[1].split("/")[0]
    assert not host.startswith(("127.", "localhost", "0.0.0.0", "[::1]")), (
        f"the recorded target host is {host!r}, which is loopback")
    assert evidence["events"], "the recording contains no network events at all"


def test_the_recording_has_not_been_edited(evidence) -> None:
    """The manifest is evidence; evidence that can be quietly edited is not."""
    import hashlib
    live = hashlib.sha256(
        MANIFEST.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert live == evidence["stamp"]["manifest_sha256"], (
        f"the manifest no longer hashes to the value recorded in stamp.json.\n"
        f"  recorded : {evidence['stamp']['manifest_sha256']}\n"
        f"  current  : {live}\n"
        f"Re-record BOTH with the instrument rather than editing either.")


# ── 2. ENDPOINTS ARE IDENTIFIED FROM REAL TRAFFIC ────────────────────────────

def test_every_endpoint_is_backed_by_events_that_really_happened(evidence) -> None:
    """No endpoint may exist that no request produced, and none may be lost.

    Both directions matter: an invented endpoint would put a route into a
    generated spec that the application does not serve, and a dropped one would
    silently narrow the API surface a spec is allowed to assert on.
    """
    from urllib.parse import urlsplit
    from app import network_evidence as ne

    observed: dict[str, int] = {}
    for event in evidence["events"]:
        parts = urlsplit(str(event.get("url") or ""))
        key = (f"{str(event.get('method') or '').upper()} "
               f"{parts.netloc}{ne.path_template(parts.path)}")
        observed[key] = observed.get(key, 0) + 1

    rows = {f"{r['method']} {r['host']}{r['path_template']}": r
            for r in evidence["inventory"]["endpoints"]}

    assert set(rows) == set(observed), (
        f"the inventory and the raw events disagree about which endpoints "
        f"exist.\n  only in inventory: {sorted(set(rows) - set(observed))}"
        f"\n  only in events   : {sorted(set(observed) - set(rows))}")
    for key, count in observed.items():
        assert rows[key]["observed_count"] == count, (
            f"{key} was observed {count} times but the inventory says "
            f"{rows[key]['observed_count']}")
    assert (sum(r["observed_count"] for r in evidence["inventory"]["endpoints"])
            == len(evidence["events"]))


def test_an_endpoints_sequence_and_timing_survive_the_manifest(evidence) -> None:
    """THE REGRESSION FOR THE DEFECT A23 FOUND, and a fixture could not.

    ``build_inventory`` read ``sequence`` and ``timestamp_ms`` with
    ``isinstance(value, int)``. That is true for an event handed straight over by
    the port and FALSE for the identical event read back off a manifest, whose
    network-event fields are typed ``dict[str, str]``. So on a real crawl's
    STORED evidence every endpoint row carried
    ``first_sequence=None last_sequence=None first_timestamp_ms=None`` — all 7 of
    them here, across 68 events.

    It matters twice: the inventory's own ordering keys on ``first_sequence`` and
    therefore fell through to its fallback for every row, and M2.4's generation
    reads this inventory, so a compiled spec could not know when an endpoint was
    first observed.

    The manifest is read from disk here on purpose. Constructing the events in
    Python would hand ints back and reproduce the exact blindness this test
    exists to prevent.
    """
    rows = evidence["inventory"]["endpoints"]
    blind = [r["path_template"] for r in rows if r["first_sequence"] is None]
    assert not blind, (
        f"{len(blind)} of {len(rows)} endpoint rows carry no first_sequence "
        f"after being built from a stored manifest: {blind}")
    for row in rows:
        assert isinstance(row["first_sequence"], int)
        assert isinstance(row["last_sequence"], int)
        assert row["first_sequence"] <= row["last_sequence"]
        assert isinstance(row["first_timestamp_ms"], int)
        assert row["first_timestamp_ms"] <= row["last_timestamp_ms"]


def test_the_inventory_is_ordered_by_first_observation(evidence) -> None:
    """A consequence of the fix above, worth its own assertion: the inventory
    reads as the funnel happened. With every ``first_sequence`` None it was
    sorted by the 1<<30 fallback and then alphabetically — a legible order, but
    not the application's."""
    firsts = [r["first_sequence"] for r in evidence["inventory"]["endpoints"]]
    assert firsts == sorted(firsts), (
        f"endpoints are not ordered by first observation: {firsts}")


# ── 3. THE ACTION JOIN ───────────────────────────────────────────────────────

def test_every_endpoint_names_the_actions_that_really_fired_it(evidence) -> None:
    """The join, checked against the endpoint's OWN events rather than trusted.

    An ``actions`` entry that no event of that endpoint carries would be an
    attribution the evidence does not support — the failure mode that matters,
    because a generated assertion would then claim a button causes a call it
    never caused.
    """
    from urllib.parse import urlsplit
    from app import network_evidence as ne

    per_key: dict[str, set[tuple[str, str, str]]] = {}
    for event in evidence["events"]:
        parts = urlsplit(str(event.get("url") or ""))
        key = (f"{str(event.get('method') or '').upper()} "
               f"{parts.netloc}{ne.path_template(parts.path)}")
        per_key.setdefault(key, set()).add((
            str(event.get("action_verb") or "").strip(),
            str(event.get("action_label") or "").strip()[:200],
            str(event.get("action_token") or ""),
        ))

    for row in evidence["inventory"]["endpoints"]:
        key = f"{row['method']} {row['host']}{row['path_template']}"
        for entry in row["actions"]:
            triple = (entry["verb"], entry["label"], entry["action_token"])
            assert triple in per_key[key], (
                f"{key} claims it was triggered by {triple}, but no event of "
                f"that endpoint carries that action. The join invented an "
                f"attribution.")


def test_page_load_traffic_is_never_attributed_to_a_button(evidence) -> None:
    """THE NEGATIVE CHECK, and the one A23 names explicitly: unrelated requests
    must not be attached to actions.

    The dangerous failure is silent and plausible — a request that a page
    NAVIGATION produced, pinned to whichever labelled control the user pressed
    most recently. A generated spec built on that would assert that clicking
    'Continue' calls an endpoint that the router prefetches on load, and would
    then go red for a reason nobody can act on.

    Measured on this application: 61 of 68 events are navigation traffic (a
    Next.js route prefetch) and **not one of them carries a click's label**; the
    7 click-attributed events all carry theirs. The two populations are cleanly
    separated on real traffic.
    """
    borrowed = [
        (e.get("url"), e.get("action_label")) for e in evidence["events"]
        if str(e.get("action_verb") or "").strip() == "navigate"
        and str(e.get("action_label") or "").strip()
    ]
    assert not borrowed, (
        f"{len(borrowed)} navigation-time requests carry a click's label, i.e. "
        f"page-load traffic attributed to a button: {borrowed[:5]}")

    unlabelled_clicks = [
        e.get("url") for e in evidence["events"]
        if str(e.get("action_verb") or "").strip() == "click"
        and not str(e.get("action_label") or "").strip()]
    assert not unlabelled_clicks, (
        f"{len(unlabelled_clicks)} click-attributed requests name no control, so "
        f"the attribution cannot be checked by a reader: {unlabelled_clicks[:5]}")

    verbs = {str(e.get("action_verb") or "").strip() for e in evidence["events"]}
    assert verbs <= {"navigate", "click", "type", "select", "check", ""}, (
        f"unexpected action verbs on the wire: {verbs}")


def test_the_join_is_deterministic_under_reordering(evidence) -> None:
    """A23 requires the joins to be DETERMINISTIC, and they were not.

    Feeding the same 68 events in a different order produced a different
    inventory. Endpoint identity, counts and statuses were stable; the
    ``actions`` list was not, and for three of the seven endpoints a shuffled run
    kept a DIFFERENT SET — ``MAX_ACTIONS_PER_ENDPOINT`` is a prefix cap, and a
    prefix of an unordered stream is arbitrary.

    ``build_inventory`` now aggregates in ``sequence`` order — the crawl-wide
    ordinal assigned at capture, which exists precisely so order can be recovered
    after the fact — so the result is a function of the event SET rather than of
    how it was delivered.

    Five seeds, not one: a single shuffle can agree by luck, particularly for an
    endpoint whose action count is under the cap.
    """
    canonical = json.dumps(evidence["inventory"], sort_keys=True)
    for seed in (1, 7, 42, 99, 12345):
        shuffled = list(evidence["events"])
        random.Random(seed).shuffle(shuffled)
        again = json.dumps(inv.build_inventory(shuffled), sort_keys=True)
        assert again == canonical, (
            f"the inventory differs when the same events arrive in a different "
            f"order (seed {seed}). The join is not deterministic.")


# ── 4. THE EVIDENCE CARRIES NO CREDENTIAL ────────────────────────────────────

def test_the_recording_carries_no_raw_credential(evidence) -> None:
    """A live crawl signs in. The evidence it leaves behind must not be a place
    to read the password back out of — this recording is committed."""
    raw = MANIFEST.read_text(encoding="utf-8")
    for pattern in ("authorization", "Authorization", "set-cookie", "Set-Cookie",
                    "bearer ", "Bearer "):
        assert pattern not in raw, (
            f"the committed manifest contains {pattern!r}")
    for event in evidence["events"]:
        assert str(event.get("auth_pattern") or "none") in (
            "none", "bearer", "basic", "cookie", "present"), (
            f"unexpected auth_pattern {event.get('auth_pattern')!r}")


def _fmt(row: Mapping[str, Any]) -> str:
    return (f"  seq[{row['first_sequence']}..{row['last_sequence']}] "
            f"{row['method']} {row['path_template']}  seen={row['observed_count']}"
            f"  statuses={row['statuses']}\n"
            + "".join(f"        triggered by: {a['verb']} {a['label']!r}\n"
                      for a in row["actions"][:4]))


def test_print_the_endpoint_map_as_evidence(evidence, capsys) -> None:
    """Not an assertion — the artifact A23 asks to be attached, printed where a
    reviewer reads CI rather than left in a file they have to go and find."""
    with capsys.disabled():
        stamp = evidence["stamp"]
        print(f"\n{'=' * 72}\nGATE 3 / A23 — ENDPOINT MAP FROM A LIVE APPLICATION"
              f"\n{'=' * 72}")
        print(f"target       : {stamp['target_url']}")
        print(f"posture      : {stamp['posture']}")
        print(f"explorer     : {stamp['explorer_version']}")
        print(f"events       : {len(evidence['events'])}   "
              f"endpoints: {evidence['inventory']['endpoint_count']}")
        verbs: dict[str, int] = {}
        for e in evidence["events"]:
            v = str(e.get("action_verb") or "") or "(none)"
            verbs[v] = verbs.get(v, 0) + 1
        print(f"attribution  : {verbs}")
        print()
        for row in evidence["inventory"]["endpoints"]:
            print(_fmt(row), end="")
        print("=" * 72)
