"""M3.3 / T-FL-03 — DURABLE object-storage handoff for crawl evidence.

THE DEFECT
==========
The explorer writes ``{work_dir}/{crawl_id}/manifest.jsonl`` plus staged PNGs and
download artifacts, and qe-central INGESTS them by reading
``{crawl_storage_root}/{crawl_id}/manifest.jsonl`` from what it assumes is the
same filesystem (``routers/internal._crawl_dir``,
``controlplane/completion_recovery._crawl_dir``). That assumption is a
single-node one. In Kubernetes the explorer's ``/work`` and qe-central's
``/work`` are **different** ``emptyDir`` volumes — pod-local, node-local, and
destroyed with the pod. So the moment producer and consumer are scheduled on
different pods:

  * ingestion finds no manifest and records ``no manifest produced``, i.e. a
    COMPLETED crawl is reported as a failure and its evidence is discarded;
  * the reaper's completion-recovery path cannot recover it either, because
    there is nothing local to recover;
  * an explorer pod restart between writing the manifest and qe-central reading
    it destroys the evidence outright — ``emptyDir`` does not survive the pod.

THIS MODULE USES THE HOUSE STORAGE LAYER — DELIBERATELY
=======================================================
An earlier revision of this file hand-rolled its own boto3 client and its own
``QEC_EVIDENCE_*`` environment contract. That was a mistake, and it is worth
recording why rather than just deleting it:

  * ``nexus_sdk.storage`` already provides exactly this — ``create_storage`` +
    ``ArtifactStore`` over s3 / gcs / azure / local — and is used by five
    engines, platform-api, **and qe-central itself** in
    ``app/substrate/assets.py``;
  * that module's own docstring states the requirement plainly: config comes
    from the SAME ``NEXUS_STORAGE_BACKEND`` env contract the other services use
    "so assets are co-readable across services — design §3.1 hard requirement".
    A second, parallel storage convention inside one service breaks precisely
    that;
  * ``ArtifactStore.build_key`` enforces a TENANT-SCOPED prefix, so a
    misconfigured service cannot write across tenant boundaries. The hand-rolled
    layout keyed on ``crawl_id`` alone and had no such property — a strictly
    weaker position on the exact axis this milestone exists to defend.

Keys therefore follow the convention ``assets.py`` already established for crawl
screenshots (the ``eyes`` namespace)::

    build_key(tenant_id, "eyes", "crawls", crawl_id) / <path relative to the crawl dir>

READ-THROUGH, NOT READ-INSTEAD
==============================
:func:`ensure_local` returns the local directory when it already has the
manifest, and only falls back to object storage otherwise. A single-node
deployment never pays a network round trip; a producer and consumer that DID
share a volume keep working during a migration; and the fetch is idempotent, so
a retried ingestion is cheap.

WHAT "UNCONFIGURED" MEANS
=========================
Only the CLOUD backends (s3 / gcs / azure) are treated as object-backed. A
``local`` backend — the default — makes every function here a no-op, so an
unconfigured deployment behaves byte-identically to the pre-M3.3 shared-volume
path and this milestone cannot regress a working single-node install.

FAIL-CLOSED ON A CONFIGURED-BUT-BROKEN STORE
============================================
If a cloud backend is selected and cannot be reached, the fetch FAILS — it does
not silently fall back to the empty local directory. Falling back would
reproduce the exact bug this module exists to fix, except now with a
configuration that claims to have fixed it.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

#: The file whose presence means "this crawl's evidence is here".
MANIFEST_FILENAME = "manifest.jsonl"

#: The ArtifactStore namespace crawl evidence lives under. Matches
#: ``app/substrate/assets.py`` (R-5), which already stores this crawl's
#: screenshots under ``eyes`` — evidence for one crawl should not be split
#: across two namespaces.
EVIDENCE_NAMESPACE = "eyes"
#: Segment separating crawl evidence from the per-session frame assets
#: ``assets.py`` writes under the same namespace.
EVIDENCE_SEGMENT = "crawls"

#: Backends whose storage is genuinely REMOTE. ``local`` is the default and is
#: treated as "not object-backed" so an unconfigured install is unchanged.
_REMOTE_BACKENDS = frozenset({"s3", "gcs", "azure"})

_store_cache = None


class EvidenceStoreError(RuntimeError):
    """The configured store could not satisfy the request.

    Deliberately NOT caught-and-ignored by the ingestion path: a configured
    store that cannot be reached must surface, because the alternative is
    ingesting an empty directory and calling a successful crawl a failure.
    """


def _store():
    """The env-configured SDK ArtifactStore (cached).

    Mirrors ``app/substrate/assets.py::_store`` exactly, including passing
    ``local_root`` EXPLICITLY rather than relying on the SDK's env default
    (which is the unwritable ``/data/nexus``).
    """
    global _store_cache
    if _store_cache is None:
        from nexus_sdk.storage import create_storage
        from nexus_sdk.storage.artifact_store import ArtifactStore
        from nexus_sdk.storage.base import StorageConfig

        cfg = StorageConfig(
            backend=settings.nexus_storage_backend,
            local_root=settings.nexus_storage_path,
        )
        _store_cache = ArtifactStore(create_storage(cfg), cfg)
    return _store_cache


def reset_store_cache_for_tests() -> None:
    """Drop the cached store so tests can re-point storage env vars.

    Same escape hatch ``assets.py`` provides, for the same reason.
    """
    global _store_cache
    _store_cache = None


def backend_name() -> str:
    try:
        return _store().backend_name
    except Exception as exc:  # pragma: no cover — misconfiguration
        logger.warning("qec.evidence.backend_resolve_failed",
                       extra={"error": str(exc)[:200]})
        return "local"


def is_object_backed() -> bool:
    """True only for a genuinely remote backend (s3 / gcs / azure).

    FAIL-CLOSED WHEN A REMOTE BACKEND IS CONFIGURED BUT CANNOT BE BUILT.
    ``backend_name`` swallows construction errors and answers "local", which is
    the right answer for a deployment that IS local — and a silent catastrophe
    for one that is not. With s3 configured but the store unbuildable (a missing
    aiobotocore, an unwritable root, a malformed endpoint), the old path made
    :func:`publish_crawl_dir` a no-op and returned 0: every crawl's evidence
    silently never published, which is the exact defect this module exists to
    fix, wearing the costume of a working local install.

    CI found this by accident. Two tests in the T-FL-03 proof hit a real
    PermissionError on the storage root and PASSED anyway, because the swallow
    turned it into "local". A false pass, in the file whose job is proving the
    handoff works.

    A deployment that configures ``local`` is unchanged: it never reaches the
    strict path.
    """
    configured = str(getattr(settings, "nexus_storage_backend", "") or "").strip().lower()
    if configured not in _REMOTE_BACKENDS:
        return False
    try:
        return _store().backend_name in _REMOTE_BACKENDS
    except Exception as exc:
        raise EvidenceStoreError(
            f"{configured!r} object storage is configured but the store could "
            f"not be built ({type(exc).__name__}: {str(exc)[:160]}). Refusing to "
            f"report 'local' and silently stop publishing crawl evidence."
        ) from exc


def evidence_prefix(tenant_id: str, crawl_id: str) -> str:
    """The TENANT-SCOPED key prefix for one crawl's evidence.

    Built through ``ArtifactStore.build_key``, so the tenant segment and the
    traversal rejection are the SDK's, not a local re-implementation. The
    explorer mirrors this layout by hand (it has no SDK dependency) and a
    contract test asserts the two agree.
    """
    return _store().build_key(
        tenant_id, EVIDENCE_NAMESPACE, EVIDENCE_SEGMENT, crawl_id)


def object_key(tenant_id: str, crawl_id: str, relative: str) -> str:
    """The key for one file of one crawl."""
    rel = str(relative).replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise EvidenceStoreError(
            f"unsafe relative path in evidence key: {relative!r}")
    return f"{evidence_prefix(tenant_id, crawl_id)}/{rel}"


# ─── Producer half ──────────────────────────────────────────────────────────


async def publish_crawl_dir(tenant_id: str, crawl_id: str,
                            local_dir: str | Path) -> int:
    """Upload every file under ``local_dir`` for ``crawl_id``. Returns the count.

    THE MANIFEST IS UPLOADED LAST, which is why this walks the tree itself
    rather than calling ``ArtifactStore.upload_directory``: that helper uploads
    concurrently, and the manifest's presence is what :func:`exists` treats as
    "this crawl's evidence is here". Publishing it before the frames it
    references would let a consumer that raced the upload ingest a manifest whose
    screenshots are still missing — a partial crawl presented as a whole one.

    A no-op on a local backend: there, the crawl directory already IS durable.
    """
    if not is_object_backed():
        return 0
    root = Path(local_dir)
    if not root.is_dir():
        raise EvidenceStoreError(f"nothing to publish: {root} is not a directory")

    store = _store()
    uploaded = 0
    manifest: Path | None = None
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name == MANIFEST_FILENAME and path.parent == root:
                manifest = path
                continue
            await store.upload_file(
                object_key(tenant_id, crawl_id,
                           path.relative_to(root).as_posix()), path)
            uploaded += 1
        if manifest is not None:
            await store.upload_file(
                object_key(tenant_id, crawl_id, MANIFEST_FILENAME), manifest)
            uploaded += 1
    except Exception as exc:
        raise EvidenceStoreError(
            f"could not publish crawl {crawl_id}: {str(exc)[:200]}") from exc
    logger.warning("qec.evidence.published",
                   extra={"crawl_id": crawl_id, "files": uploaded,
                          "backend": backend_name()})
    return uploaded


# ─── Consumer half ──────────────────────────────────────────────────────────


async def exists(tenant_id: str, crawl_id: str) -> bool:
    """Is this crawl's evidence present in object storage (manifest uploaded)?"""
    if not is_object_backed():
        return False
    try:
        return await _store().exists(
            object_key(tenant_id, crawl_id, MANIFEST_FILENAME))
    except Exception:
        return False


async def fetch_crawl_dir(tenant_id: str, crawl_id: str,
                          dest: str | Path) -> int:
    """Download every object for ``crawl_id`` into ``dest``. Returns the count."""
    if not is_object_backed():
        return 0
    store = _store()
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    prefix = evidence_prefix(tenant_id, crawl_id) + "/"
    fetched = 0
    try:
        for key in await store.backend.list_objects(prefix, max_keys=10_000):
            rel = key[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            target = root / rel
            # Containment: a crafted key must not escape the destination.
            if not str(target.resolve()).startswith(str(root.resolve())):
                logger.error("qec.evidence.unsafe_key_skipped",
                             extra={"crawl_id": crawl_id, "key": key})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(await store.download_bytes(key))
            fetched += 1
    except Exception as exc:
        raise EvidenceStoreError(
            f"could not fetch crawl {crawl_id} evidence: {str(exc)[:200]}") from exc
    logger.info("qec.evidence.fetched",
                extra={"crawl_id": crawl_id, "files": fetched})
    return fetched


async def ensure_local(tenant_id: str, crawl_id: str,
                       local_dir: str | Path) -> Path:
    """Guarantee ``local_dir`` holds this crawl's evidence; return the path.

    THE SEAM the ingestion path calls. Read-through:

      * the manifest is already local (single node, a shared volume, or a
        previous fetch)  → return immediately, zero network;
      * otherwise, on a remote backend → materialise it;
      * otherwise → return the local path unchanged, so a local backend behaves
        exactly as it always did and the caller's existing "no manifest
        produced" handling still applies.

    A configured-but-unreachable store RAISES rather than returning an empty
    directory: silently returning nothing would recreate the very bug this
    module exists to fix, while claiming to have fixed it.
    """
    root = Path(local_dir)
    if (root / MANIFEST_FILENAME).is_file():
        return root
    if not is_object_backed():
        return root
    fetched = await fetch_crawl_dir(tenant_id, crawl_id, root)
    if fetched == 0 and not (root / MANIFEST_FILENAME).is_file():
        logger.warning(
            "qec.evidence.not_in_object_store",
            extra={"crawl_id": crawl_id,
                   "detail": "no objects under this crawl's prefix — the "
                             "producer never published, or published elsewhere"})
    return root


__all__ = [
    "EVIDENCE_NAMESPACE", "EVIDENCE_SEGMENT", "MANIFEST_FILENAME",
    "EvidenceStoreError", "backend_name", "ensure_local", "evidence_prefix",
    "exists", "fetch_crawl_dir", "is_object_backed", "object_key",
    "publish_crawl_dir", "reset_store_cache_for_tests",
]
