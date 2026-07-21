#!/usr/bin/env python3
"""Deploy-verification gate — prove the DEPLOYED code equals git.

Phase 0's reproducibility guarantee is "a clean build == what runs." This proves
it directly: it content-hashes every runtime file in a git source tree and in the
deployed copy (a running container, or an image/dir), and fails if they differ.
When a deploy runs this and it passes, "what runs" is provably the git checkout —
which is the whole point of retiring ad-hoc ``docker cp`` patching.

Line endings are normalized (``\\r`` stripped) before hashing, so a CRLF checkout
and an LF container are not reported as different — only real content changes are.

Compare a running container against git::

    verify_deployment.py --git-root platform/api/app \\
        --container nexus-platform-api --container-path /app/service/app

Compare an extracted image / directory against git::

    verify_deployment.py --git-root platform/api/app --deployed-root /tmp/img/app

Exit code is non-zero if any runtime file is missing, extra, or different.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

# Runtime file kinds a rebuild must reproduce byte-for-byte (source we ship).
DEFAULT_SUFFIXES: tuple[str, ...] = (".py",)
# Never part of a reproducible comparison (build caches / vendored / vcs).
_SKIP_DIRS = frozenset({"__pycache__", ".git", "node_modules", ".venv", "venv",
                        ".mypy_cache", ".pytest_cache"})


def _content_hash(path: str) -> str:
    """SHA-256 of a file's content with line endings normalized to ``\\n``."""
    with open(path, "rb") as fh:
        data = fh.read()
    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def manifest(root: str, suffixes: tuple[str, ...]) -> dict[str, str]:
    """``{relative_path: normalized_sha256}`` for every runtime file under root."""
    out: dict[str, str] = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith(suffixes):
                continue
            p = os.path.join(base, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            try:
                out[rel] = _content_hash(p)
            except OSError:
                out[rel] = "<unreadable>"
    return out


def compare(git_root: str, deployed_root: str, suffixes: tuple[str, ...]
            ) -> tuple[list[str], list[str], list[str]]:
    g = manifest(git_root, suffixes)
    d = manifest(deployed_root, suffixes)
    only_git = sorted(set(g) - set(d))
    only_deployed = sorted(set(d) - set(g))
    differ = sorted(p for p in (set(g) & set(d)) if g[p] != d[p])
    return only_git, only_deployed, differ


def _extract_container(container: str, container_path: str) -> str:
    """``docker cp`` the container's code to a temp dir; return that dir."""
    tmp = tempfile.mkdtemp(prefix="deployverify_")
    dest = os.path.join(tmp, "deployed")
    # docker cp <c>:<path>/. <dest>  copies the DIRECTORY CONTENTS into dest.
    subprocess.run(
        ["docker", "cp", f"{container}:{container_path.rstrip('/')}/.", dest],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--git-root", required=True, help="git source dir to verify against")
    ap.add_argument("--deployed-root", help="extracted image / directory to compare")
    ap.add_argument("--container", help="running container name to compare (uses docker cp)")
    ap.add_argument("--container-path", help="path INSIDE the container matching --git-root")
    ap.add_argument("--suffix", action="append", default=[],
                    help="runtime file suffix(es) to compare (default: .py)")
    args = ap.parse_args(argv)

    suffixes = tuple(args.suffix) or DEFAULT_SUFFIXES
    cleanup = None
    if args.container:
        if not args.container_path:
            ap.error("--container requires --container-path")
        deployed = _extract_container(args.container, args.container_path)
        cleanup = os.path.dirname(deployed)
    elif args.deployed_root:
        deployed = args.deployed_root
    else:
        ap.error("supply --deployed-root or --container")

    try:
        only_git, only_deployed, differ = compare(args.git_root, deployed, suffixes)
    finally:
        if cleanup and os.path.isdir(cleanup):
            shutil.rmtree(cleanup, ignore_errors=True)

    total = len(only_git) + len(only_deployed) + len(differ)
    label = args.container or args.deployed_root
    if total == 0:
        print(f"deploy-verify PASS: {args.git_root} == {label} (content-identical)")
        return 0
    print(f"deploy-verify FAIL: {args.git_root} != {label}", file=sys.stderr)
    for p in differ:
        print(f"  differs:       {p}")
    for p in only_git:
        print(f"  only in git:   {p}  (deployed is missing it)")
    for p in only_deployed:
        print(f"  only deployed: {p}  (runs, but not in git)")
    print(f"deploy-verify: {total} file(s) do not match — 'what runs' is NOT this git tree.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
