"""TEAM A / PHASE A — PER-CRAWL egress fence files (G2 / R2, the real fix).

Frozen wire shape: ``Nexus_power/contracts/fleet_egress_fence_v1.json``.
Read that file first — it names all three parties. This module is the
qe-central PRODUCER half only.

WHAT WAS WRONG BEFORE. ``_write_egress_allowlist`` wrote ONE file per worker
(``allowed_domains.txt``) and squid matched a single ``dstdomain`` ACL against
it for every request. Two crawls concurrently dispatched to one worker shared
that file, so the last writer re-fenced the other tenant's live browser — the
cross-tenant leak the strict xfail in ``tests/fleet/test_t_fl_08`` recorded and
the ``FENCE_IS_PER_WORKER`` clamp made unreachable.

WHY PER-CRAWL FILES ALONE WOULD FIX NOTHING (the ARB record's finding, adopted
here): the constraint was in the CONSUMER. squid applied one ACL to every
request because it had no way to tell crawl A's request from crawl B's. The
missing per-request identity is the PROXY LOGIN: each browser context is
created with proxy credentials ``username = crawl_id`` (explorer side), squid
authenticates it with ``basic_fake_auth`` (any password — the username is the
identity), and the generated ACL pair below allows crawl X to reach ONLY the
domains in crawl X's own file. The fence is therefore selected per request, by
the crawl that made it, however many crawls share the worker.

LAYOUT, under the DIRECTORY of the worker's registered ``allowlist_path``
(``fence root``; qe-central's mount of the worker's squid allowlist volume):

    crawls/allowlist.<crawl_id>.txt   one dstdomain file per live crawl
    crawls.conf                       generated: acl pair + one allow per crawl
    reload.stamp                      content changes on every regeneration;
                                      the proxy container HUPs squid on change

The legacy per-worker ``allowed_domains.txt`` is NOT written and NOT read any
more. An old squid.conf that still reads it therefore denies everything —
fail-closed during a mixed deploy, never a silently shared fence.

ORDERING RULES (each write is atomic via tmp + ``os.replace``):

  * ADD:    write the crawl's file FIRST, then regenerate ``crawls.conf``, then
    bump the stamp — a HUP between any two steps reads a config whose
    referenced files all exist.
  * REMOVE: regenerate ``crawls.conf`` WITHOUT the crawl first, bump the stamp,
    THEN unlink the file — squid never reloads a config naming a missing file.

GC: a crawl file older than ``QEC_EGRESS_FENCE_MAX_AGE_SECONDS`` (default 4h —
comfortably past every wall budget + grace) is dropped at the next
regeneration, so a crash between dispatch and completion cannot grow the ACL
set without bound. Completion, dispatch failure and the reaper all release the
fence explicitly; the age GC is the backstop, not the mechanism.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

#: Contract layout names (fleet_egress_fence_v1.json — change there first).
PER_CRAWL_DIR = "crawls"
ACL_INCLUDE = "crawls.conf"
RELOAD_STAMP = "reload.stamp"
_PREFIX, _SUFFIX = "allowlist.", ".txt"

#: The path PREFIX squid sees the fence root at (the same volume qe-central
#: writes). The generated crawls.conf must name squid's view, not ours.
ENV_SQUID_ROOT = "QEC_EGRESS_SQUID_ROOT"
_DEFAULT_SQUID_ROOT = "/etc/squid/allowlist"

#: Backstop GC age for orphaned per-crawl fence files.
ENV_FENCE_MAX_AGE = "QEC_EGRESS_FENCE_MAX_AGE_SECONDS"
_DEFAULT_FENCE_MAX_AGE_S = 4 * 3600.0

#: The identity squid keys the fence on. A crawl id that cannot appear safely
#: in a generated squid.conf line or a filename is refused outright.
CRAWL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,49}$")

#: One domain per dstdomain line; refuse anything that could smuggle a second
#: line or an ACL token into a generated file. (Upstream validation is the real
#: gate — this is the defence at the writing edge.)
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9.\-_*]{1,253}$")


class FenceError(ValueError):
    """A fence write that must abort the dispatch (mapped to HTTP upstream)."""


def squid_root() -> str:
    return (os.environ.get(ENV_SQUID_ROOT, "") or _DEFAULT_SQUID_ROOT).rstrip("/")


def fence_max_age_seconds() -> float:
    try:
        return float(os.environ.get(ENV_FENCE_MAX_AGE, "") or _DEFAULT_FENCE_MAX_AGE_S)
    except (TypeError, ValueError):
        return _DEFAULT_FENCE_MAX_AGE_S


def fence_root(allowlist_path: str) -> Path:
    """The worker's fence directory — the parent of its registered
    ``allowlist_path`` (qec_022 column, unchanged), so per-worker isolation and
    the T-FL-04 shared-path refusal keep exactly their existing key."""
    return Path(allowlist_path).parent


def crawl_fence_path(allowlist_path: str, crawl_id: str) -> Path:
    return fence_root(allowlist_path) / PER_CRAWL_DIR / f"{_PREFIX}{crawl_id}{_SUFFIX}"


def _tag(crawl_id: str) -> str:
    """The squid ACL-name-safe form of a crawl id (contract ``tag_rule``)."""
    return re.sub(r"[^A-Za-z0-9]", "_", crawl_id)


def _atomic_write(path: Path, body: str) -> None:
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    tmp.write_text(body, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _crawl_id_of(path: Path) -> str:
    name = path.name
    if not (name.startswith(_PREFIX) and name.endswith(_SUFFIX)):
        return ""
    return name[len(_PREFIX):-len(_SUFFIX)]


def regenerate(allowlist_path: str) -> list[str]:
    """Rebuild ``crawls.conf`` from the per-crawl files on disk; bump the stamp.

    Returns the crawl ids the new config fences (for logging/tests). Age-GCs
    orphaned files first, so a crashed crawl cannot keep its egress permission
    forever. Never called with a half-written file in view: every producer
    writes atomically.
    """
    root = fence_root(allowlist_path)
    crawl_dir = root / PER_CRAWL_DIR
    crawl_dir.mkdir(parents=True, exist_ok=True)

    import time
    now = time.time()
    max_age = fence_max_age_seconds()
    fenced: list[str] = []
    lines: list[str] = [
        "# generated by qe-central (controlplane/scheduling/egress_fence.py)",
        "# one ACL pair + one allow per LIVE crawl - contracts/fleet_egress_fence_v1.json",
    ]
    for path in sorted(crawl_dir.glob(f"{_PREFIX}*{_SUFFIX}")):
        cid = _crawl_id_of(path)
        if not cid or not CRAWL_ID_RE.match(cid):
            continue                      # never let a stray file reach the conf
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > max_age:
            try:
                path.unlink()
                logger.warning("qec.egress_fence.aged_out",
                               extra={"crawl_id": cid, "age_s": int(age)})
            except OSError:
                pass
            continue
        tag = _tag(cid)
        squid_file = f"{squid_root()}/{PER_CRAWL_DIR}/{_PREFIX}{cid}{_SUFFIX}"
        lines.append(f"acl crawl_{tag} proxy_auth {cid}")
        lines.append(f'acl fence_{tag} dstdomain "{squid_file}"')
        lines.append(f"http_access allow crawl_{tag} fence_{tag}")
        fenced.append(cid)

    _atomic_write(root / ACL_INCLUDE, "\n".join(lines) + "\n")
    # The stamp's CONTENT changes every time (uuid, not mtime): the proxy-side
    # watcher compares content, so two regenerations inside one second cannot
    # be collapsed into a missed reload the way an mtime compare could.
    _atomic_write(root / RELOAD_STAMP, uuid.uuid4().hex + "\n")
    return fenced


def write_crawl_fence(domains: list[str], allowlist_path: str, *, crawl_id: str) -> Path:
    """Write crawl ``crawl_id``'s own fence file and publish it to squid.

    Raises :class:`FenceError` for an empty domain list, an unsafe domain, or a
    crawl id that cannot be keyed — all of which must ABORT the dispatch: a
    browser must never launch behind a fence that was not written.
    """
    if not CRAWL_ID_RE.match(crawl_id or ""):
        raise FenceError(f"crawl id {crawl_id!r} cannot key an egress fence")
    cleaned = [str(d or "").strip() for d in (domains or [])]
    cleaned = [d for d in cleaned if d]
    if not cleaned:
        raise FenceError("no destination domains - refusing to write an empty fence")
    for d in cleaned:
        if not _DOMAIN_RE.match(d):
            raise FenceError(f"unsafe allowlist entry {d!r}")

    path = crawl_fence_path(allowlist_path, crawl_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (f"# fence for crawl {crawl_id} (written by qe-central at dispatch, fail-closed)\n"
            + "\n".join(cleaned) + "\n")
    _atomic_write(path, body)
    fenced = regenerate(allowlist_path)
    logger.info("qec.egress_fence.written",
                extra={"crawl_id": crawl_id, "domains": len(cleaned),
                       "live_fences": len(fenced)})
    return path


def release_crawl_fence(allowlist_path: str, crawl_id: str) -> bool:
    """Remove one crawl's fence: conf first (without it), then the file.

    Best-effort and never raises — releasing a fence is cleanup, and a cleanup
    failure must not convert a finished crawl into an error. Returns whether a
    fence file existed.
    """
    try:
        if not CRAWL_ID_RE.match(crawl_id or ""):
            return False
        path = crawl_fence_path(allowlist_path, crawl_id)
        existed = path.exists()
        if existed:
            # Remove-order rule: the conf must stop naming the file BEFORE the
            # file goes, or a HUP in between would read a config that names a
            # missing dstdomain file.
            try:
                path.rename(path.with_name(path.name + ".releasing"))
            except OSError:
                pass
            regenerate(allowlist_path)
            for stale in path.parent.glob(f"{_PREFIX}{crawl_id}{_SUFFIX}*"):
                try:
                    stale.unlink()
                except OSError:
                    pass
        return existed
    except Exception as exc:  # pragma: no cover - cleanup must never raise
        logger.warning("qec.egress_fence.release_failed",
                       extra={"crawl_id": crawl_id, "error": str(exc)[:200]})
        return False


async def release_crawl_fence_everywhere(crawl_id: str) -> int:
    """Release ``crawl_id``'s fence from EVERY known worker root.

    Used at completion/reap, where the caller knows the crawl but not reliably
    the worker (an old row, a worker that re-registered). Roots come from the
    live registry plus the static pool; the set is tiny and the operation is a
    couple of small file writes per root. Never raises.
    """
    roots: dict[str, str] = {}
    try:
        from . import worker_registry
        for w in await worker_registry.list_workers():
            ap = str(w.get("allowlist_path") or "").strip()
            if ap:
                roots[str(fence_root(ap))] = ap
    except Exception:
        pass
    try:
        from ...clients.config import phase1_settings
        for w in phase1_settings.workers() or ():
            ap = str((w or {}).get("allowlist_path") or "").strip()
            if ap:
                roots[str(fence_root(ap))] = ap
    except Exception:
        pass
    released = 0
    for ap in roots.values():
        if release_crawl_fence(ap, crawl_id):
            released += 1
    if released:
        logger.info("qec.egress_fence.released",
                    extra={"crawl_id": crawl_id, "roots": released})
    return released
