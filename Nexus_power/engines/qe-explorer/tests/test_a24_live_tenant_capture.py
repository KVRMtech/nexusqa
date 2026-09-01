"""GATE 3 / A24 — the M2.6 capture fixes, on a LIVE TENANT APPLICATION.

M2.6 is proven on fixtures and on ``proving-grounds/acme-life``. Both are
first-party, and acme-life was **edited for M2.6** — it grew an accordion and a
``<details>`` so the expansion pass would have something to open. That is the
right way to prove a mechanism and it cannot answer A24's question, which is
whether the fixes hold on an application nobody shaped for them.

THE EVIDENCE
============
``Nexus_power/evidence/a24_live_capture/`` — a crawl of
**https://vkpowerlife.136-85-106-73.sslip.io/**, a deployed tenant application on
a real VM over real HTTPS, recorded by ``record_live_capture.py`` through the
production ``Crawler`` and ``PlaywrightBrowserPort`` with **no boundary approvals
and no walk attestation**. ``stop_reason=completed``, 9 states, 2 flows, 19
distinct controls, 0 inventory failures.

WHAT THIS TENANT PROVES, AND WHAT IT CANNOT
===========================================
It exercises T-CAP-01 hard and T-CAP-03 only negatively, and both are asserted
below as what they are:

* **The option ceiling is really gone.** ``State of residence`` on this live
  application offers **52 options**, and the capture reports
  ``options_total == 52`` with all 52 present. That number matters: the defect
  M2.6 verified was a stack of private ceilings (browser snippet 300, Python
  refiner 60, **catalogue 48**), and 52 > 48. This one live control would have
  been silently clipped by the catalogue ceiling alone.

* **The expansion pass costs nothing here — because there is nothing to open.**
  ``expansions_opened``, ``expansions_skipped`` and ``tab_views_recorded`` are
  all 0. That is only meaningful if the application genuinely declares no
  collapsed UI, so it is not assumed: capture emits ``disclosure`` for every
  action it records, and across all 40 recorded actions the value is ``""`` — no
  ``<details>``, no ``aria-expanded``, no ``role=tab``. The counters read zero
  because the page had nothing shut, not because the pass failed to look.

  **T-CAP-03's positive path is therefore NOT proven on this tenant.** It is
  proven on acme-life and on the M2.6 fixtures. Finding a live tenant with a real
  accordion is the remaining work, and it is named here rather than papered over.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = SERVICE_ROOT.parent.parent / "evidence" / "a24_live_capture"

#: The control that makes this tenant worth crawling for T-CAP-01, and the
#: ceiling it would have hit. Named as constants so the assertion below reads as
#: the claim rather than as two magic numbers.
LARGE_SELECT = "State of residence"
OLD_CATALOGUE_CEILING = 48


def _load(name: str) -> Any:
    path = EVIDENCE / name
    assert path.is_file(), (
        f"the A24 live-tenant evidence is missing from {path}.\n"
        f"Re-record it:\n"
        f"  cd engines/qe-explorer && QEC_MEASURE_USER=... QEC_MEASURE_PASSWORD=... "
        f"QEC_MEASURE_OTP=... python record_live_capture.py <tenant url>\n"
        f"There is deliberately no fixture fallback — A24's whole claim is that "
        f"the capture fixes hold on an application nobody shaped for them.")
    text = path.read_text(encoding="utf-8")
    if name.endswith(".jsonl"):
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


@pytest.fixture(scope="module")
def capture() -> dict[str, Any]:
    coverage = _load("coverage.json")
    controls: dict[str, Any] = {}
    for state in (coverage.get("states") or []):
        for name, signal in (state.get("form_snapshot_signals") or {}).items():
            controls.setdefault(str(name), signal)
    actions = [a for record in _load("manifest.jsonl")
               if isinstance(record.get("actions"), list)
               for a in record["actions"] if isinstance(a, dict)]
    return {
        "coverage": coverage,
        "controls": controls,
        "actions": actions,
        "stamp": _load("stamp.json"),
    }


# ── SUBJECT PRESENCE ─────────────────────────────────────────────────────────
#
# MEASURED, by feeding this module a structurally valid capture of a crawl that
# saw NOTHING — zero states, zero controls, zero actions: six of its nine
# assertions passed. Every claim here is about a POPULATION (controls, answer
# sets, recorded actions), and a claim about an empty population is true and
# worthless.
#
# nexusqa-9e's question is the fix: WOULD THIS STILL PASS IF THE SUBJECT WERE
# ABSENT? Where the answer was yes and the test makes a claim, the population it
# needs is now asserted first.
def _require(condition: bool, what: str) -> None:
    assert condition, (
        f"SUBJECT ABSENT: {what}. The assertion that follows would pass "
        f"vacuously, so it is refused rather than reported green. Re-record "
        f"with record_live_capture.py.")


# ── IS IT REAL, AND IS IT INTACT? ────────────────────────────────────────────

def test_the_capture_is_of_a_live_tenant_over_the_internet(capture) -> None:
    target = str(capture["stamp"]["target_url"])
    assert target.startswith("https://"), (
        f"A24 requires a live tenant over real HTTPS; recorded target {target!r}")
    host = target.split("//", 1)[1].split("/")[0]
    assert not host.startswith(("127.", "localhost", "0.0.0.0")), (
        f"recorded target host {host!r} is loopback — that is a fixture crawl")


def test_the_recording_has_not_been_edited(capture) -> None:
    live = hashlib.sha256(
        (EVIDENCE / "coverage.json").read_text(encoding="utf-8")
        .encode("utf-8")).hexdigest()
    assert live == capture["stamp"]["coverage_sha256"], (
        "the coverage account no longer hashes to the value in stamp.json — "
        "re-record it rather than editing it")


def test_the_crawl_really_read_the_application(capture) -> None:
    """A capture proof over a crawl that read nothing proves nothing.

    ``inventory_failures`` is asserted at zero because a page that would not read
    is precisely the case where a control count means nothing.
    """
    cov = capture["coverage"]
    assert len(cov.get("states") or []) >= 5, (
        f"only {len(cov.get('states') or [])} states were observed")
    assert cov.get("flows"), "the crawl walked no journey"
    assert not cov.get("auth_blocked"), (
        f"the crawl never got in: {cov.get('auth_blocked_reason')}")
    assert int(cov.get("inventory_failures") or 0) == 0, (
        f"{cov.get('inventory_failures')} page(s) would not read: "
        f"{cov.get('inventory_failure_detail')}")
    assert len(capture["controls"]) >= 15, (
        f"only {len(capture['controls'])} distinct controls were captured")


# ── T-CAP-01: THE OPTION CEILING, ON REAL DATA ───────────────────────────────

def test_a_real_tenants_large_select_survives_every_layer(capture) -> None:
    """The headline T-CAP-01 claim, on an application nobody wrote for it.

    The defect M2.6 verified was a stack of PRIVATE ceilings — browser snippet
    300, Python refiner 60, catalogue 48 — each of which silently truncated an
    answer set while reporting a complete one. ``State of residence`` on this
    live tenant offers 52, which clears the smallest of them by four.
    """
    controls = capture["controls"]
    assert LARGE_SELECT in controls, (
        f"the crawl did not reach {LARGE_SELECT!r}; captured: "
        f"{sorted(controls)}")
    signal = controls[LARGE_SELECT]
    options = list(signal.get("options") or [])
    total = int(signal.get("options_total") or 0)

    assert total > OLD_CATALOGUE_CEILING, (
        f"{LARGE_SELECT!r} offers only {total} options on this tenant, which is "
        f"at or below the old catalogue ceiling of {OLD_CATALOGUE_CEILING}. The "
        f"assertion below would then pass without proving the ceiling is gone — "
        f"point A24 at an application with a larger select.")
    assert len(options) == total, (
        f"{LARGE_SELECT!r} reports options_total={total} but carries "
        f"{len(options)} options. A clipped enumeration stored as a complete one "
        f"is exactly the defect T-CAP-01 exists to prevent.")


def test_no_captured_control_is_a_clipped_answer_set_reported_as_complete(
    capture,
) -> None:
    """The same rule across every control on the tenant, not just the big one.

    ``options_total`` is what the browser COUNTED; ``options`` is what survived
    the wire. They may legitimately differ — that is what makes a clip visible —
    but on this application nothing was clipped, and any future divergence must
    be a deliberate, reported one rather than a silent truncation.
    """
    with_options = [n for n, s in capture["controls"].items() if s.get("options")]
    _require(len(with_options) >= 3,
             f"only {len(with_options)} control(s) carry an answer set, so "
             f"'nothing was clipped' is a claim about almost nothing")
    clipped = {
        name: (len(sig.get("options") or []), int(sig.get("options_total") or 0))
        for name, sig in capture["controls"].items()
        if sig.get("options")
        and len(sig["options"]) != int(sig.get("options_total") or 0)
    }
    assert not clipped, (
        f"controls whose carried options disagree with the counted total: "
        f"{clipped}")


def test_every_captured_control_declares_a_verified_locator(capture) -> None:
    """A catalogued question that cannot point at its control is not reviewable
    against the application — the reason ``locator`` became a catalogue column in
    qec_019."""
    _require(len(capture["controls"]) >= 10,
             f"only {len(capture['controls'])} controls captured")
    missing = [name for name, sig in capture["controls"].items()
               if not (sig.get("locator") or {}).get("strategy")]
    assert not missing, f"controls captured with no locator strategy: {missing}"
    unverified = [name for name, sig in capture["controls"].items()
                  if not (sig.get("locator") or {}).get("verified")]
    assert not unverified, (
        f"controls whose locator was never verified against the page: "
        f"{unverified}")


# ── T-CAP-03: THE NEGATIVE CASE, PROVEN NEGATIVE ─────────────────────────────

def test_the_expansion_pass_paid_nothing_because_nothing_was_shut(capture) -> None:
    """Zero expansions is only evidence if the page really had nothing to open.

    A crawl that failed to notice a collapsed accordion and a crawl that met an
    application with none are indistinguishable in the counters alone — which is
    exactly why M2.6 made capture emit ``disclosure`` for every control it
    records, normalised across ``<details>``, ``aria-expanded`` and
    ``role=tab``.

    So the counters are checked TOGETHER with the declarations: 0 opened, and 0
    controls declaring themselves collapsed.
    """
    cov = capture["coverage"]
    counters = {k: int(cov.get(k) or 0) for k in
                ("expansions_opened", "expansions_skipped", "tab_views_recorded")}
    disclosures = {str((a.get("qec") or {}).get("disclosure", ""))
                   for a in capture["actions"]}

    assert disclosures, "no action carried a disclosure field at all"
    if counters["expansions_opened"] == 0:
        assert disclosures == {""}, (
            f"the expansion pass opened nothing, but capture DID declare "
            f"collapsed UI on this application: {sorted(disclosures)}. Zero "
            f"expansions is then a miss, not an absence.")
        assert counters["expansions_skipped"] == 0, (
            f"nothing was opened and nothing was declared collapsed, yet "
            f"{counters['expansions_skipped']} expansions are reported skipped")


def test_the_disclosure_field_is_emitted_for_every_recorded_action(capture) -> None:
    """The field must be PRESENT even when empty. An absent field and an empty
    one are the same to a reader and opposite to a gate: absent means this build
    cannot see collapsed UI at all, empty means it looked and found none."""
    _require(len(capture["actions"]) >= 10,
             f"only {len(capture['actions'])} recorded actions — 'every action "
             f"carries disclosure' would be near-vacuous, and this test is what "
             f"makes the zero-expansion result meaningful")
    absent = [a.get("target_label") for a in capture["actions"]
              if "disclosure" not in (a.get("qec") or {})]
    assert not absent, (
        f"{len(absent)} recorded actions carry no `disclosure` key, so this "
        f"build cannot distinguish 'nothing was collapsed' from 'collapsed UI is "
        f"invisible to capture': {absent[:6]}")


def test_print_the_live_tenant_capture_as_evidence(capture, capsys) -> None:
    with capsys.disabled():
        stamp = capture["stamp"]
        cov = capture["coverage"]
        print(f"\n{'=' * 72}\nGATE 3 / A24 — M2.6 CAPTURE ON A LIVE TENANT"
              f"\n{'=' * 72}")
        print(f"target      : {stamp['target_url']}")
        print(f"posture     : {stamp['posture']}")
        print(f"states      : {stamp['states']}   flows: {stamp['flows']}   "
              f"controls: {len(capture['controls'])}")
        print(f"expansions  : opened={cov.get('expansions_opened')} "
              f"skipped={cov.get('expansions_skipped')} "
              f"tab_views={cov.get('tab_views_recorded')}   "
              f"(declared collapsed: none)")
        print("\ncontrols carrying an answer set:")
        rows = sorted(
            ((n, len(s.get("options") or []), int(s.get("options_total") or 0))
             for n, s in capture["controls"].items() if s.get("options")),
            key=lambda r: -r[1])
        for name, carried, total in rows:
            flag = "  <-- over the old catalogue ceiling of 48" if total > 48 else ""
            print(f"  {name:26} carried={carried:3}  counted={total:3}{flag}")
        print("=" * 72)
