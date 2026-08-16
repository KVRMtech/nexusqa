#!/usr/bin/env python3
"""DEPLOYMENT MANIFEST — the record of what was deployed, and the only input to
rollback.

WHY THIS EXISTS (M0.4 / T-GT-01, T-GT-02).
``deploy.ps1`` built its docker-compose command in two blocks and reused ONE
variable for both::

    if ($qecBuild.Count -gt 0) { $svcList = $qecBuild -join " "  ... }
    if ($mainBuild.Count -gt 0) { $svcList = $mainBuild -join " " ... }

``$svcList`` is script-scoped, so ``Invoke-GateRollback`` — defined later, reading
the same variable — saw whatever the LAST block wrote. On the default deploy
(qe-central + qe-explorer + platform-api) that is ``platform-api``. A red gate
therefore rolled back ONE of three services, printed "Fleet restored", and
ended the investigation while two containers kept serving the rejected build.
The drill did not catch it because the drill hardcoded a single service too.

The fix is not a bigger variable. It is to stop deriving rollback targets from
MUTABLE state at rollback time. The deployment inventory is computed ONCE, before
anything is built, written to a manifest, and rollback restores exactly that set
— from the compose file the manifest RECORDS, not from a guess made later.

ORDERING. Deploy order is the manifest order. Rollback order is its REVERSE
(LIFO): the last thing swapped in is the first thing swapped out, and the
backend a service depends on is restored before the service that calls it.
Both orders are explicit in the plan so a drill can assert them.

ALL-OR-REPORT. The old rollback set ``ok=1`` if ANY service restored, so a
partial rollback exited 0. Here every service in the manifest must be restored;
anything less is a FAILED rollback that names the survivors.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

MANIFEST_VERSION = 1
DEFAULT_MANIFEST = ".deploy_manifest.json"

# The compose file that OWNS each service. A service is deployed and restored
# through the same file — deriving it twice is how a rollback ends up running
# `docker compose -f the-wrong-file up` and reporting success on a no-op.
SERVICE_COMPOSE = {
    "qe-central": "docker-compose.qec.yml",
    "qe-explorer": "docker-compose.qec.yml",
    "platform-api": "docker-compose.yml",
}

# Deploy order. Not alphabetical: qe-central and qe-explorer share the qec
# overlay and are brought up together, and platform-api (the factory backend)
# goes last so the services that call it are already on the new build.
DEPLOY_ORDER = ("qe-central", "qe-explorer", "platform-api")


class ManifestError(ValueError):
    """A manifest that cannot be trusted to drive a rollback."""


def compose_for(service: str) -> str:
    try:
        return SERVICE_COMPOSE[service]
    except KeyError:
        raise ManifestError(
            "unknown service %r — valid: %s"
            % (service, ", ".join(sorted(SERVICE_COMPOSE)))) from None


def build_manifest(services, *, commit: str = "", deployed_at: str = "") -> dict:
    """The deployment inventory, computed ONCE.

    Deduplicates, rejects unknown services, and orders by ``DEPLOY_ORDER`` so the
    manifest is a deterministic function of the requested set — the same request
    always yields byte-identical bytes, which is what makes a manifest auditable
    rather than merely present."""
    requested = [str(s).strip() for s in (services or []) if str(s).strip()]
    if not requested:
        raise ManifestError("a deployment with no services is not a deployment")
    unknown = sorted({s for s in requested if s not in SERVICE_COMPOSE})
    if unknown:
        raise ManifestError(
            "unknown service(s): %s — valid: %s"
            % (", ".join(unknown), ", ".join(sorted(SERVICE_COMPOSE))))
    seen: set[str] = set()
    ordered = [s for s in DEPLOY_ORDER if s in requested and not (s in seen or seen.add(s))]
    return {
        "manifest_version": MANIFEST_VERSION,
        "commit": str(commit or ""),
        "deployed_at": str(deployed_at or ""),
        "services": [
            {"name": name, "compose": compose_for(name), "order": i + 1}
            for i, name in enumerate(ordered)
        ],
    }


def load_manifest(path: str) -> dict:
    """Read and VALIDATE a manifest.

    Raises rather than degrading: a rollback driven by a manifest we could not
    parse would restore an arbitrary subset, which is the precise failure this
    module exists to prevent. Better to abort loudly and let a human roll back by
    hand than to report a rollback that restored the wrong set."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ManifestError("no deployment manifest at %s" % path) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("unreadable deployment manifest %s: %s" % (path, exc)) from None
    if not isinstance(data, dict):
        raise ManifestError("manifest %s is not an object" % path)
    if int(data.get("manifest_version") or 0) != MANIFEST_VERSION:
        raise ManifestError(
            "manifest %s is version %r, this tool speaks version %d"
            % (path, data.get("manifest_version"), MANIFEST_VERSION))
    services = data.get("services")
    if not isinstance(services, list) or not services:
        raise ManifestError("manifest %s records no services" % path)
    for entry in services:
        if not isinstance(entry, dict) or not str(entry.get("name") or "").strip():
            raise ManifestError("manifest %s has a malformed service entry" % path)
        if not str(entry.get("compose") or "").strip():
            raise ManifestError(
                "manifest %s: service %r records no compose file"
                % (path, entry.get("name")))
    return data


def write_manifest(path: str, manifest: dict) -> None:
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)


def deploy_plan(manifest: dict) -> list[dict]:
    """Services in DEPLOY order."""
    return sorted(manifest["services"], key=lambda e: int(e.get("order") or 0))


def rollback_plan(manifest: dict) -> list[dict]:
    """Services in ROLLBACK order — the reverse of deploy (LIFO)."""
    return list(reversed(deploy_plan(manifest)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="write a deployment manifest")
    p_build.add_argument("--out", required=True)
    p_build.add_argument("--commit", default="")
    p_build.add_argument("--deployed-at", default="")
    p_build.add_argument("services", nargs="+")

    p_plan = sub.add_parser("rollback-plan",
                            help="print 'service<TAB>compose' in rollback order")
    p_plan.add_argument("--manifest", required=True)

    p_dplan = sub.add_parser("deploy-plan",
                             help="print 'service<TAB>compose' in deploy order")
    p_dplan.add_argument("--manifest", required=True)

    p_show = sub.add_parser("services", help="print deployed service names")
    p_show.add_argument("--manifest", required=True)

    args = ap.parse_args(argv)
    try:
        if args.command == "build":
            manifest = build_manifest(args.services, commit=args.commit,
                                      deployed_at=args.deployed_at)
            write_manifest(args.out, manifest)
            print(" ".join(e["name"] for e in deploy_plan(manifest)))
            return 0
        manifest = load_manifest(args.manifest)
        if args.command == "rollback-plan":
            rows = rollback_plan(manifest)
        elif args.command == "deploy-plan":
            rows = deploy_plan(manifest)
        else:
            print(" ".join(e["name"] for e in deploy_plan(manifest)))
            return 0
        for entry in rows:
            print("%s\t%s" % (entry["name"], entry["compose"]))
        return 0
    except ManifestError as exc:
        sys.stderr.write("MANIFEST ERROR: %s\n" % exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
