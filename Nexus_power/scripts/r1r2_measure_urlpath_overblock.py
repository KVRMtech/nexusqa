"""R1/R2 - THE URL-PATH OVER-BLOCK: why two of three Phase-1 funnels dead-end.

WHAT THIS MEASURES
==================
``rp.verb.pay`` and ``rp.verb.underwrite`` both carry
``applies_to: ["button_name", "url_path", "url_query"]``. Because ``url_path``
is in that list, the rule fires on the PAGE a control lives on, not on the
control. So EVERY actuator on a page whose URL contains "payment" or
"underwriting" is classified ``danger=critical`` - the Back button and the
notification bell included. A danger-flagged control is removed from every
advance tier, so the funnel below that page becomes unwalkable.

    vkpower-life  /life-insurance/apply/payment/   -> rp.verb.pay
                  seals payment -> beneficiary -> signature -> confirmation.
                  The commit control "Sign & Submit Application" lives on
                  /signature/, so it is never reached.

    summit-life-carrier
                  /underwriting/new-business/new-application
                                                   -> rp.verb.underwrite
                  seals the wizard, and the nav link INTO it ("New Business
                  Queue") is itself flagged because its href carries the path.

THE PACK ALREADY SAYS THIS IS WRONG - AND SAYS IT WAS FIXED
===========================================================
``refuse_pack.yaml`` (allow_overrides, the A14 row) states:

    "This is the SAME over-block that already forced `rp.verb.underwrite` to be
     scoped off `url_path`, where it had marked 20 of 35 controls on
     /underwriting/new-business/new-application as critical - including the Back
     button and the notification bell."

**That claim is false against the rule it describes.** ``rp.verb.underwrite``
still lists ``url_path`` (refuse_pack.yaml:160), and
``git log -S'rp.verb.underwrite'`` shows it was never removed. The comment
documents a remediation that was never applied.

WHY THIS SCRIPT DOES NOT FIX IT
===============================
Removing ``url_path`` from a ``critical`` refuse rule widens what the crawler is
willing to click across EVERY tenant and application. That is a security
decision with blast radius far beyond Gate 2, and this repository's own
precedent for exactly this situation (commit 735e6b4, the first allow_overrides
row) was a NARROW, full-string-anchored override scoped to ``button_name`` ONLY,
whose rationale states "never a url - no GET can be unblocked by it". So the
in-doctrine remedy is an owner's decision, not an edit made to turn a crawl
green.

This script MEASURES the defect and proves the diagnosis discriminates. It
changes nothing.

USAGE
    python r1r2_measure_urlpath_overblock.py [--json OUT.json]

    exit 0  the over-block reproduced AND the control group is intact
    exit 1  the measurement itself is broken

FAIL-CLOSED. Exit 0 means "the defect reproduced as described". If the control
group ever stops holding - if scoping ``url_path`` off would ALSO unblock a
genuine commit verb - this exits 1, because then the diagnosis would be wrong
and the proposed remedy unsafe.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile

EXPLORER = pathlib.Path(__file__).resolve().parents[1] / "engines" / "qe-explorer"
REPO_ROOT = EXPLORER.parents[2]
sys.path.insert(0, str(EXPLORER))

from app.guard import load_refuse_pack             # noqa: E402
from app.inventory import classify_control_danger  # noqa: E402

PACK = EXPLORER / "app" / "refuse_pack.yaml"
PAY_URL = "http://127.0.0.1:8101/life-insurance/apply/payment/"
UW_URL = "http://127.0.0.1:8103/underwriting/new-business/new-application"
NEUTRAL = "http://127.0.0.1:8101/x/"

#: Controls that COMMIT NOTHING and are flagged only because of the page URL.
#: Each was read from the application's own source.
OVERBLOCKED = [
    ("Back", PAY_URL, "router.back() - navigates"),
    ("Notifications", PAY_URL, "header bell - opens a panel"),
    ("Monthly", PAY_URL, "billing-frequency selector - sets local state"),
    ("Annual", PAY_URL, "billing-frequency selector - sets local state"),
    ("Credit / Debit Card", PAY_URL, "payment-METHOD selector - sets local state"),
    ("Continue to Beneficiary Designation", PAY_URL,
     "router.push to /apply/beneficiary/ - navigates"),
    ("New Business Queue", UW_URL, "left-nav route link - navigates"),
    ("Back", UW_URL, "wizard back - navigates"),
]

#: THE CONTROL GROUP. Scoping url_path off must NOT unblock any of these. If it
#: does, the remedy is unsafe and this script fails.
MUST_STAY_REFUSED = [
    "Sign & Submit Application", "Pay Now", "Make Payment",
    "Submit to Underwriting", "Transfer Funds", "Underwrite Now",
]


def _counterfactual_pack():
    """The same pack with url_path/url_query scoped OFF the two rules only."""
    src = PACK.read_text(encoding="utf-8")
    out, cur = [], None
    for line in src.splitlines(keepends=True):
        m = re.match(r'\s*- id: "([^"]+)"', line)
        if m:
            cur = m.group(1)
        if (cur in ("rp.verb.pay", "rp.verb.underwrite")
                and "applies_to:" in line and "url_path" in line):
            line = re.sub(r"applies_to:.*", 'applies_to: ["button_name"]', line)
        out.append(line)
    tmp = pathlib.Path(tempfile.mkdtemp()) / "refuse_pack_nourl.yaml"
    tmp.write_text("".join(out), encoding="utf-8")
    return load_refuse_pack(str(tmp))


def _produced_by() -> dict:
    def run(args):
        try:
            done = subprocess.run(args, cwd=str(REPO_ROOT), capture_output=True,
                                  text=True, timeout=30)
            return done.stdout.strip() if done.returncode == 0 else ""
        except Exception:
            return ""
    head = run(["git", "rev-parse", "HEAD"])
    # Scoped to the code that PRODUCES this measurement - the pack under test,
    # the classifier it is read through, and this script. Deliberately excludes
    # the evidence directory: an emitted artifact must not report its own
    # emission as tree drift. Same rule gate2_journey.py::_producing_code uses.
    dirty = run(["git", "status", "--porcelain", "--",
                 "Nexus_power/engines/qe-explorer/app",
                 "Nexus_power/scripts/r1r2_measure_urlpath_overblock.py"])
    return {"head": head or "(unknown)", "dirty": bool(dirty),
            "dirty_paths": [ln[3:].strip() for ln in dirty.splitlines()][:20],
            "scope": "qe-explorer/app + this script"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    base = load_refuse_pack(str(PACK))
    cf = _counterfactual_pack()
    rows, broken = [], []

    print("OVER-BLOCKED BY url_path (each control commits nothing):")
    print("  %-38s %-32s %s" % ("CONTROL", "BASELINE", "url_path SCOPED OFF"))
    for name, url, why in OVERBLOCKED:
        b = classify_control_danger(name, "button", "button", base, url)
        c = classify_control_danger(name, "button", "button", cf, url)
        rows.append({
            "control": name, "url": url, "why_harmless": why,
            "baseline": {"danger": b[0], "rule": b[1], "severity": b[2]},
            "counterfactual": {"danger": c[0], "rule": c[1], "severity": c[2]},
        })
        print("  %-38s %-32s %s" % (name[:36], "%s %s" % (b[0], b[1]),
                                    "%s %s" % (c[0], c[1])))
        if not b[0]:
            broken.append("%s was expected danger at baseline and is not" % name)

    print("")
    print("CONTROL GROUP - must STAY refused with url_path scoped off:")
    control_rows = []
    for name in MUST_STAY_REFUSED:
        c = classify_control_danger(name, "button", "button", cf, NEUTRAL)
        control_rows.append({"control": name, "still_refused": bool(c[0]),
                             "rule": c[1]})
        print("  %-30s %s %s" % (name, c[0], c[1]))
        if not c[0]:
            broken.append("CONTROL GROUP BREACH: %s would be unblocked" % name)

    doc = {
        "finding": "rp.verb.pay and rp.verb.underwrite are scoped to url_path, "
                   "so every actuator on a payment/underwriting page is critical",
        "produced_by": _produced_by(),
        "pack_version": getattr(base, "version", "?"),
        "false_claim_in_pack":
            "refuse_pack.yaml allow_overrides states rp.verb.underwrite was "
            "'already forced to be scoped off url_path'; line 160 still lists "
            "url_path and git log -S shows it never was",
        "overblocked": rows,
        "control_group": control_rows,
        "blocks": {
            "R1 vkpower-life":
                "payment -> beneficiary -> signature sealed; commit control "
                "'Sign & Submit Application' never reached",
            "R2 summit-life-carrier":
                "underwriting wizard sealed; the nav link into it is flagged",
        },
        "second_independent_blocker_R1":
            "'ACH Bank Transfer Direct debit from checking or savings' also "
            "trips rp.verb.transfer on its own BUTTON NAME, so scoping url_path "
            "off does NOT by itself unblock vkpower-life's payment step",
    }
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(doc, indent=2),
                                           encoding="utf-8")
        print("")
        print("evidence -> %s" % args.json)

    if broken:
        print("")
        print("MEASUREMENT BROKEN:")
        for b in broken:
            print("  * %s" % b)
        return 1
    print("")
    print("OVER-BLOCK REPRODUCED, control group intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
