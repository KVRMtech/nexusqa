"""Tamper-evident export manifest for the Execution Evidence Report (spec §2.17).

"Immutable" is a mechanism here, not an adjective:

  * every file in an export is hashed (SHA-256);
  * the hashes are folded into ONE ``chain_root`` by a documented, trivially
    reproducible rule, so changing any byte of any file changes the root;
  * the root carries an OPTIONAL detached HMAC signature — present only when a
    signing key is configured. With no key we say ``signed: false`` and call it
    tamper-EVIDENT, never tamper-proof. We do not fake a signature we cannot
    make (that would be exactly the kind of unverifiable claim this whole
    report exists to eliminate);
  * a dependency-free verifier script ships INSIDE the export, so a regulator
    can re-verify offline, years later, without our software.

Mirrors the philosophy of the Part-11 heal ledger (``diff_and_heal.heal_evidence``):
chain for evidence, optional detached signature for proof, honest degradation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

MANIFEST_VERSION = "nexus-evidence-manifest/v1"
MANIFEST_FILENAME = "manifest.json"
VERIFIER_FILENAME = "verify_evidence.py"

_SIGNING_ENV = "NEXUS_EVIDENCE_SIGNING_KEY"
#: Preferred over the env var for on-prem: a secret in a FILE can be mounted,
#: rotated and permission-controlled without recreating the container, and it
#: does not leak into `docker inspect`, crash dumps or a process listing.
_SIGNING_KEY_FILE_ENV = "NEXUS_EVIDENCE_SIGNING_KEY_FILE"
_DEFAULT_KEY_PATH = "/run/secrets/nexus_evidence_signing_key"


def _signing_key() -> str:
    """The signing secret, from a key FILE if present, else the env var.

    File first, deliberately. Returns "" when neither is configured — and that
    absence is reported honestly as ``signed: false`` rather than papered over
    with a locally-derived pseudo-key, which would look like a signature while
    proving nothing an attacker with code access could not also produce.
    """
    path = (os.getenv(_SIGNING_KEY_FILE_ENV, "") or "").strip() or _DEFAULT_KEY_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
        if key:
            return key
    except Exception:
        pass          # no key file is a normal, supported state
    return (os.getenv(_SIGNING_ENV, "") or "").strip()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def chain_root(entries: list[dict]) -> str:
    """Fold per-file hashes into one root.

    Deliberately simple so the shipped verifier (and any third party) can
    reimplement it in a few lines::

        root = sha256(MANIFEST_VERSION)
        for path in sorted(paths):
            root = sha256(root + "|" + path + "|" + sha256(file_bytes))

    Sorting by path makes the root independent of packing order.
    """
    root = hashlib.sha256(MANIFEST_VERSION.encode("utf-8")).hexdigest()
    for e in sorted(entries, key=lambda x: str(x.get("path", ""))):
        blob = f"{root}|{e.get('path', '')}|{e.get('sha256', '')}"
        root = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return root


def signing_enabled() -> bool:
    return bool(_signing_key())


def sign_root(root: str) -> str:
    """Detached HMAC-SHA256 over the chain root, or "" when no key is set.

    Swapping in an asymmetric private-key signature is a drop-in here: nothing
    else in the manifest depends on how the root is signed.
    """
    key = _signing_key()
    if not key or not root:
        return ""
    return hmac.new(key.encode("utf-8"), root.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_root_signature(root: str, signature: str) -> bool | None:
    """True/False when a key is configured; None when signing is off or the
    manifest carries no signature (cannot assert — which is NOT a pass)."""
    if not signing_enabled() or not signature:
        return None
    return hmac.compare_digest(sign_root(root), str(signature))


def signing_key_source() -> str:
    """WHERE the key came from — surfaced so an operator can confirm the intended
    secret is in play rather than assuming it."""
    path = (os.getenv(_SIGNING_KEY_FILE_ENV, "") or "").strip() or _DEFAULT_KEY_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read().strip():
                return f"file:{path}"
    except Exception:
        pass
    return f"env:{_SIGNING_ENV}" if (os.getenv(_SIGNING_ENV, "") or "").strip() else "none"


def build_manifest(files: dict[str, bytes], *, meta: dict | None = None) -> dict:
    """Hash every exported file, fold to a root, sign if a key is configured."""
    entries = [
        {"path": path, "sha256": sha256_hex(blob), "bytes": len(blob)}
        for path, blob in sorted(files.items())
    ]
    root = chain_root(entries)
    sig = sign_root(root)
    return {
        "manifest_version": MANIFEST_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "meta": dict(meta or {}),
        "file_count": len(entries),
        "total_bytes": sum(e["bytes"] for e in entries),
        "files": entries,
        "chain_root": root,
        "signature": sig,
        "signed": bool(sig),
        "algorithm": {"file_digest": "sha256", "chain": "sha256-fold-sorted-by-path",
                      "signature": "hmac-sha256-detached" if sig else "none",
                      "key_source": signing_key_source()},
        "verification": (
            "Recompute sha256 of each listed file, fold them per `algorithm.chain` "
            "and compare with chain_root. Any single changed byte changes the root. "
            "Run the bundled verify_evidence.py — it needs only the Python standard "
            "library."
        ),
        "honesty_note": (
            "signed=false means NO signing key was configured at export time: this "
            "package is tamper-EVIDENT (the chain detects edits) but not "
            "tamper-PROOF (someone who can rewrite the whole package could also "
            "recompute the root). We do not emit a signature we cannot make."
        ),
    }


def verify_manifest(files: dict[str, bytes], manifest: dict) -> dict:
    """Re-verify an export in-process (the shipped script does the same offline).

    Fails LOUDLY and specifically: which file, expected vs actual digest.
    """
    listed = {str(e.get("path", "")): e for e in (manifest.get("files") or [])}
    present = dict(files)
    present.pop(MANIFEST_FILENAME, None)     # the manifest never hashes itself

    mismatched, missing, extra = [], [], []
    for path, entry in listed.items():
        blob = present.get(path)
        if blob is None:
            missing.append(path)
            continue
        actual = sha256_hex(blob)
        if actual != entry.get("sha256"):
            mismatched.append({"path": path, "expected_sha256": entry.get("sha256"),
                               "actual_sha256": actual,
                               "expected_bytes": entry.get("bytes"),
                               "actual_bytes": len(blob)})
    for path in present:
        if path not in listed and path != VERIFIER_FILENAME:
            extra.append(path)

    recomputed = chain_root([
        {"path": p, "sha256": sha256_hex(b)} for p, b in present.items()
        if p in listed
    ] + [{"path": p, "sha256": listed[p].get("sha256")} for p in missing])
    root_ok = bool(recomputed == manifest.get("chain_root"))
    sig_ok = verify_root_signature(str(manifest.get("chain_root") or ""),
                                   str(manifest.get("signature") or ""))
    ok = root_ok and not mismatched and not missing
    reasons = []
    if mismatched:
        reasons.append(f"{len(mismatched)} file(s) modified after export")
    if missing:
        reasons.append(f"{len(missing)} file(s) missing from the package")
    if not root_ok:
        reasons.append("chain root does not match the manifest")
    if sig_ok is False:
        ok = False
        reasons.append("detached signature does NOT match the chain root")
    return {
        "ok": ok,
        "chain_root_ok": root_ok,
        "expected_chain_root": manifest.get("chain_root"),
        "recomputed_chain_root": recomputed,
        "signature_verified": sig_ok,
        "mismatched": mismatched,
        "missing": missing,
        "unlisted_extra": extra,
        "reasons": reasons or (["package verified — every file matches its recorded digest"]
                               if ok else ["verification failed"]),
    }


#: Shipped INSIDE every export. Standard library only, no network, no install —
#: an auditor can run it on an air-gapped machine years from now.
VERIFIER_SCRIPT = '''#!/usr/bin/env python3
"""Offline verifier for a VKPower Execution Evidence export.

Usage:
    python verify_evidence.py [directory]     # defaults to the current directory

Exits 0 only when EVERY listed file matches its recorded SHA-256 and the folded
chain root matches. Any tampering exits non-zero and names the offending file.
Standard library only — no install, no network.
"""
import hashlib
import json
import os
import sys

MANIFEST_VERSION = "nexus-evidence-manifest/v1"


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def chain_root(entries):
    root = hashlib.sha256(MANIFEST_VERSION.encode("utf-8")).hexdigest()
    for e in sorted(entries, key=lambda x: x["path"]):
        blob = "%s|%s|%s" % (root, e["path"], e["sha256"])
        root = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return root


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "."
    mpath = os.path.join(base, "manifest.json")
    if not os.path.exists(mpath):
        print("FAIL: manifest.json not found in %s" % base)
        return 2
    with open(mpath, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    problems = []
    recomputed = []
    for entry in manifest.get("files", []):
        rel = entry["path"]
        full = os.path.join(base, rel)
        if not os.path.exists(full):
            problems.append("MISSING   %s" % rel)
            recomputed.append({"path": rel, "sha256": entry["sha256"]})
            continue
        actual = sha256_file(full)
        recomputed.append({"path": rel, "sha256": actual})
        if actual != entry["sha256"]:
            problems.append("MODIFIED  %s\\n            expected %s\\n            actual   %s"
                            % (rel, entry["sha256"], actual))

    root = chain_root(recomputed)
    root_ok = (root == manifest.get("chain_root"))
    if not root_ok:
        problems.append("CHAIN ROOT MISMATCH\\n            expected %s\\n            actual   %s"
                        % (manifest.get("chain_root"), root))

    print("VKPower evidence verification")
    print("  package    : %s" % os.path.abspath(base))
    print("  files      : %d" % len(manifest.get("files", [])))
    print("  chain root : %s" % manifest.get("chain_root"))
    print("  signed     : %s" % ("yes" if manifest.get("signed") else
                                 "no (tamper-EVIDENT only - see honesty_note)"))
    if problems:
        print("\\nRESULT: FAILED - this package does NOT match its manifest")
        for p in problems:
            print("  %s" % p)
        return 1
    print("\\nRESULT: VERIFIED - every file matches its recorded digest")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


__all__ = [
    "MANIFEST_VERSION", "MANIFEST_FILENAME", "VERIFIER_FILENAME", "VERIFIER_SCRIPT",
    "sha256_hex", "chain_root", "signing_enabled", "sign_root", "signing_key_source",
    "verify_root_signature", "build_manifest", "verify_manifest",
]
