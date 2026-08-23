#!/usr/bin/env python3
"""Negative-control suite for gate5_verify_ceremony.py.

A validator that refuses everything is indistinguishable from a validator that
works, and the real ceremony record is currently REFUSED -- so running it
against the real record proves nothing about whether the checks discriminate.

This builds ONE fully-valid synthetic record and proves it PASSES (the positive
control: the validator is capable of saying yes), then breaks exactly one thing
at a time and proves each break is caught with the right reason.

Every case here corresponds to a stop condition in the Gate 5 brief.

    python scripts/gate5_verify_ceremony_selftest.py

Exit 0  every control behaved as specified
Exit 1  a control did not fire, or the positive control failed
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
VALIDATOR = os.path.join(HERE, "gate5_verify_ceremony.py")

SHA = "a" * 40
OTHER_SHA = "b" * 40


def person(name: str) -> dict:
    return {
        "name": name,
        "email": "%s@example.org" % name.lower().replace(" ", "."),
        "signed_at": "2026-08-21T12:00:00Z",
        "attests": "I have reviewed the evidence package against the certified SHA.",
    }


def valid_record() -> dict:
    return {
        "ceremony": "gate5-arb-certification",
        "certified_repository": "KVRMtech/nexusqa",
        "certified_sha": SHA,
        "build_artifact": "nexus_power-qe-central:%s" % SHA[:12],
        "deployment": {"environment": "verdict-box", "version_sha": SHA},
        "clean_clone_attestation": {
            "attestation": "gate5-clean-clone",
            "certified_sha": SHA,
            "verified_clone_sha": SHA,
            "result": "pass",
            "runner_environment": "github-hosted",
            "author_controlled_hardware": False,
            "workflow_run_url": "https://github.com/KVRMtech/nexusqa/actions/runs/1",
        },
        "gates": {g: "PASS" for g in ["gate0", "gate1", "gate2", "gate3", "gate4"]},
        "a37": {"a37_1": "PASS", "a37_2": "PASS", "a37_3": "PASS"},
        "reproductions": [
            {
                "proof": "A37.1 credentials decrypt under KMS",
                "implementer": "Alice Implementer",
                "reproducer": "Bob Reproducer",
                "sha": SHA,
                "environment": "verdict-box asia-southeast1-a",
                "result": "PASS",
                "artifact": "run-1/a37_kms.log",
            }
        ],
        "quorum_required": 3,
        "roles": {
            "release_director": person("Dana Director"),
            "proof_guild": person("Pat Guild"),
        },
        "arb_signatories": [person("Sam One"), person("Sue Two"), person("Sid Three")],
        "certification_date": "2026-08-21",
        "certification_outcome": "APPROVED",
    }


def run(rec: dict) -> tuple[int, str]:
    fd, path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        p = subprocess.run(
            [sys.executable, VALIDATOR, path],
            capture_output=True, text=True,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    finally:
        os.unlink(path)


# (label, mutator, substring that must appear in the refusal)
def _sha_is_branch(r): r["certified_sha"] = "feat/qec-dynamic-catalog-p0-p6"
def _deploy_drift(r): r["deployment"]["version_sha"] = OTHER_SHA
def _self_hosted(r): r["clean_clone_attestation"]["runner_environment"] = "self-hosted"
def _author_hw(r): r["clean_clone_attestation"]["author_controlled_hardware"] = True
def _clone_failed(r): r["clean_clone_attestation"]["result"] = "fail"
def _clone_missing(r): r["clean_clone_attestation"] = {}
def _clone_wrong_sha(r): r["clean_clone_attestation"]["certified_sha"] = OTHER_SHA
def _gate_unmet(r): r["gates"]["gate4"] = "NO_ACCEPTANCE_EVIDENCE"
def _a37_unmet(r): r["a37"]["a37_2"] = "PARTIAL"
def _no_repro(r): r["reproductions"] = []
def _self_repro(r): r["reproductions"][0]["reproducer"] = r["reproductions"][0]["implementer"]
def _repro_other_sha(r): r["reproductions"][0]["sha"] = OTHER_SHA
def _director_signs(r): r["arb_signatories"][0] = r["roles"]["release_director"]
def _quorum_short(r): r["arb_signatories"] = r["arb_signatories"][:2]
def _dupe_signatory(r): r["arb_signatories"][1] = r["arb_signatories"][0]
def _unsigned_seat(r): r["roles"]["release_director"]["name"] = ""
def _placeholder_seat(r): r["roles"]["proof_guild"]["name"] = "<name>"
def _no_signatories(r): r["arb_signatories"] = []


CONTROLS = [
    ("SHA is a branch name", _sha_is_branch, "not a full 40-hex commit SHA"),
    ("deployment differs from certified SHA", _deploy_drift, "STOP CERTIFICATION"),
    ("clean clone ran on a self-hosted runner", _self_hosted, "not 'github-hosted'"),
    ("clean clone admits author-controlled hardware", _author_hw, "author_controlled_hardware"),
    ("clean clone did not pass", _clone_failed, "not 'pass'"),
    ("clean clone attestation absent", _clone_missing, "clean_clone_attestation is missing"),
    ("clean clone is for a different SHA", _clone_wrong_sha, "is not the certified SHA"),
    ("a gate is unmet", _gate_unmet, "gates.gate4"),
    ("an A37 item is unmet", _a37_unmet, "a37.a37_2"),
    ("no reproductions at all", _no_repro, "reproductions[] is empty"),
    ("implementer reproduced their own work", _self_repro, "same person"),
    ("reproduction ran at a different SHA", _repro_other_sha, "not the certified SHA"),
    ("release director is also an ARB signatory", _director_signs, "must be independent"),
    ("ARB quorum not met", _quorum_short, "quorum not met"),
    ("same signatory counted twice", _dupe_signatory, "same person more than once"),
    ("a seat is unsigned", _unsigned_seat, "release_director.name"),
    ("a seat still holds a placeholder", _placeholder_seat, "proof_guild.name"),
    ("nobody signed at all", _no_signatories, "arb_signatories[] is empty"),
]


def main() -> int:
    failures = []

    # ── POSITIVE CONTROL ─────────────────────────────────────────────
    print("== positive control: a complete, honest ceremony must PASS")
    code, out = run(valid_record())
    if code == 0 and "GATE5_CEREMONY: VALID" in out:
        print("   PASS  the validator is capable of saying yes")
    else:
        print("   FAIL  a fully valid record was refused (exit %d)" % code)
        print("   " + "\n   ".join(out.splitlines()[:20]))
        failures.append("positive control")

    # ── NEGATIVE CONTROLS ────────────────────────────────────────────
    print("")
    print("== negative controls: each break must be caught, and named")
    for label, mutate, expect in CONTROLS:
        rec = valid_record()
        mutate(rec)
        code, out = run(rec)
        if code != 1:
            print("   FAIL  %-46s not refused (exit %d)" % (label, code))
            failures.append(label)
        elif expect not in out:
            print("   FAIL  %-46s refused, but not for the right reason" % label)
            print("         expected to see: %s" % expect)
            failures.append(label)
        else:
            print("   PASS  %s" % label)

    print("")
    if failures:
        print("SELFTEST: FAIL -- %d control(s) did not behave: %s"
              % (len(failures), ", ".join(failures)))
        return 1
    print("SELFTEST: PASS -- 1 positive control, %d negative controls" % len(CONTROLS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
