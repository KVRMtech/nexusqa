#!/usr/bin/env python3
"""GATE 5 / A37.4 -- validate an ARB certification ceremony record.

WHY A VALIDATOR AND NOT JUST A DOCUMENT.
A signature block in prose is decorative: nothing stops a certification record
from being published with three empty name fields, a SHA that never existed, a
deployment pointing somewhere else, and a clean-clone attestation from the
author's own laptop. Every one of those has a matching stop condition in the
Gate 5 brief, and every one is mechanically checkable. This refuses the record
instead of rendering it.

WHAT IT REFUSES ON (each maps to a brief stop condition):
  * certified_sha is not one immutable 40-hex commit
  * the deployment does not match the certified SHA
  * the clean-clone attestation is missing, is for a different SHA, did not
    pass, or came from hardware an author controls
  * any gate or A37 item is not PASS
  * a required role is unfilled, or still holds a placeholder
  * ARB quorum is not met
  * the same person fills two seats that must be independent
  * a critical proof has no non-author reproduction, or names the same person
    as implementer and reproducer

THE AUTHORSHIP PROBLEM THIS ENCODES.
Every commit in this repository carries an identical author, committer and
Co-Authored-By trailer, so "who wrote this" is unanswerable from git. Non-author
reproduction therefore CANNOT be reconstructed from the log afterwards -- the
record has to name the implementer and the reproducer explicitly, at the time,
or the claim is unfalsifiable. That is why reproductions[] carries names rather
than commit references.

Exit 0  the ceremony is valid and the SHA may be certified
Exit 1  REFUSED -- every reason is printed
Exit 2  the record could not be read at all

Usage:
    python scripts/gate5_verify_ceremony.py QECentral/certification/gate5_ceremony_record.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys

SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# A record is not "filled in" because a key exists. These are the values people
# leave behind when a ceremony is staged but never actually held.
PLACEHOLDERS = {
    "", "tbd", "todo", "fixme", "xxx", "pending", "unsigned", "none",
    "name", "your name", "signature", "n/a-pending",
}

REQUIRED_GATES = ["gate0", "gate1", "gate2", "gate3", "gate4"]
REQUIRED_A37 = ["a37_1", "a37_2", "a37_3"]
# Seats that must be held by different people. A Release Director who is also
# the sole ARB signatory is one person certifying their own release.
INDEPENDENT_SEATS = ["release_director", "proof_guild"]


def is_placeholder(v) -> bool:
    if v is None:
        return True
    if not isinstance(v, str):
        return False
    s = v.strip().lower()
    if s in PLACEHOLDERS:
        return True
    # <name>, <email@example.com>, {{signatory}}
    return bool(re.match(r"^[<{\[].*[>}\]]$", s))


def person_key(p: dict) -> str:
    return "%s|%s" % (
        str(p.get("name", "")).strip().lower(),
        str(p.get("email", "")).strip().lower(),
    )


def check_person(label: str, p, fail) -> bool:
    if not isinstance(p, dict):
        fail("%s: not filled in (expected an object with name/email/signed_at/attests)" % label)
        return False
    ok = True
    for field in ("name", "email", "signed_at", "attests"):
        if is_placeholder(p.get(field)):
            fail("%s.%s is empty or still a placeholder" % (label, field))
            ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("record", help="path to the ceremony record JSON")
    args = ap.parse_args()

    try:
        with open(args.record, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("FATAL: cannot read %s: %r" % (args.record, exc))
        return 2

    reasons: list[str] = []

    def fail(msg: str) -> None:
        reasons.append(msg)

    print("Gate 5 ceremony record : %s" % args.record)

    # ── 1. the anchor ────────────────────────────────────────────────
    sha = str(rec.get("certified_sha", "")).strip()
    if not SHA_RE.match(sha):
        fail("certified_sha %r is not a full 40-hex commit SHA "
             "(a branch, tag, prefix or empty value cannot be certified)" % sha)
        sha = ""
    else:
        print("certified SHA          : %s" % sha)

    # ── 2. the deployment must BE that SHA ───────────────────────────
    dep = rec.get("deployment") or {}
    dep_sha = str(dep.get("version_sha", "")).strip()
    if is_placeholder(dep_sha):
        fail("deployment.version_sha is empty -- the deployed build is unidentified")
    elif sha and dep_sha != sha:
        fail("deployment.version_sha (%s) differs from certified_sha (%s) -- "
             "STOP CERTIFICATION per the brief" % (dep_sha[:12], sha[:12]))

    # ── 3. clean clone, on hardware no author controls ───────────────
    att = rec.get("clean_clone_attestation")
    if not isinstance(att, dict) or not att:
        fail("clean_clone_attestation is missing -- there is no proof the commit "
             "builds for anyone but its author")
    else:
        a_sha = str(att.get("certified_sha", "")).strip()
        if sha and a_sha != sha:
            fail("clean_clone_attestation.certified_sha (%s) is not the certified SHA (%s)"
                 % (a_sha[:12] or "<empty>", sha[:12]))
        if str(att.get("result", "")).lower() != "pass":
            fail("clean_clone_attestation.result is %r, not 'pass'" % att.get("result"))
        if att.get("author_controlled_hardware") is not False:
            fail("clean_clone_attestation.author_controlled_hardware is not false -- "
                 "the verification must run where no author has shell")
        env = str(att.get("runner_environment", "")).strip().lower()
        if env != "github-hosted":
            fail("clean_clone_attestation.runner_environment is %r, not 'github-hosted' -- "
                 "a self-hosted runner is author-controlled hardware" % att.get("runner_environment"))
        if is_placeholder(att.get("workflow_run_url")):
            fail("clean_clone_attestation.workflow_run_url is empty -- the run is not auditable")

    # ── 4. every gate and every A37 item must be PASS ────────────────
    gates = rec.get("gates") or {}
    for g in REQUIRED_GATES:
        v = str(gates.get(g, "")).strip().upper()
        if not v:
            fail("gates.%s has no verdict recorded" % g)
        elif v != "PASS":
            fail("gates.%s is %s -- Gate 5 cannot certify over an unmet gate" % (g, v))

    a37 = rec.get("a37") or {}
    for k in REQUIRED_A37:
        v = str(a37.get(k, "")).strip().upper()
        if not v:
            fail("a37.%s has no verdict recorded" % k)
        elif v != "PASS":
            fail("a37.%s is %s" % (k, v))

    # ── 5. non-author reproduction, by NAME ──────────────────────────
    reps = rec.get("reproductions")
    if not isinstance(reps, list) or not reps:
        fail("reproductions[] is empty -- no critical proof has been independently "
             "reproduced, and git cannot supply this after the fact")
    else:
        for i, r in enumerate(reps):
            tag = "reproductions[%d]" % i
            if not isinstance(r, dict):
                fail("%s is not an object" % tag)
                continue
            for field in ("proof", "implementer", "reproducer", "sha", "environment", "result", "artifact"):
                if is_placeholder(r.get(field)):
                    fail("%s.%s is empty or a placeholder" % (tag, field))
            impl = str(r.get("implementer", "")).strip().lower()
            repro = str(r.get("reproducer", "")).strip().lower()
            if impl and repro and impl == repro:
                fail("%s: implementer and reproducer are the same person (%s) -- "
                     "'it passed for me' is not independent evidence"
                     % (tag, r.get("implementer")))
            r_sha = str(r.get("sha", "")).strip()
            if sha and SHA_RE.match(r_sha) and r_sha != sha:
                fail("%s was reproduced at %s, not the certified SHA %s"
                     % (tag, r_sha[:12], sha[:12]))
            if str(r.get("result", "")).strip().upper() not in ("", "PASS"):
                fail("%s.result is %s" % (tag, r.get("result")))

    # ── 6. the seats ─────────────────────────────────────────────────
    roles = rec.get("roles") or {}
    seat_people: dict[str, str] = {}
    for seat in INDEPENDENT_SEATS:
        p = roles.get(seat)
        if check_person("roles.%s" % seat, p, fail):
            seat_people[seat] = person_key(p)

    signatories = rec.get("arb_signatories")
    quorum = rec.get("quorum_required")
    if not isinstance(quorum, int) or quorum < 1:
        fail("quorum_required must be a positive integer")
        quorum = None
    if not isinstance(signatories, list) or not signatories:
        fail("arb_signatories[] is empty -- the ARB has not signed")
    else:
        valid = []
        for i, s in enumerate(signatories):
            if check_person("arb_signatories[%d]" % i, s, fail):
                valid.append(s)
        keys = [person_key(s) for s in valid]
        if len(set(keys)) != len(keys):
            fail("arb_signatories[] contains the same person more than once")
        if quorum is not None and len(set(keys)) < quorum:
            fail("ARB quorum not met: %d distinct signatories, %d required"
                 % (len(set(keys)), quorum))
        # A Release Director cannot also make up the ARB quorum.
        for seat, key in seat_people.items():
            if key in keys:
                fail("roles.%s is also an ARB signatory -- that seat must be "
                     "independent of the board it reports to" % seat)

    # ── verdict ──────────────────────────────────────────────────────
    print("")
    if reasons:
        print("CEREMONY REFUSED -- %d condition(s) unmet:" % len(reasons))
        for r in reasons:
            print("  * %s" % r)
        print("")
        print("GATE5_CEREMONY: REFUSED")
        return 1

    print("all ceremony conditions satisfied")
    print("GATE5_CEREMONY: VALID for %s" % sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
