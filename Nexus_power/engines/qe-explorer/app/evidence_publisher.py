"""M3.3 / T-FL-03 — PRODUCER half of the durable evidence handoff.

The explorer writes a crawl's manifest, staged frames and download artifacts to
``{work_dir}/{crawl_id}/``. In Kubernetes that directory is a POD-LOCAL
``emptyDir``: it is not visible to the qe-central pod that must ingest it, and
it does not survive this pod restarting. So on a real fleet the evidence of a
completed crawl was unreadable by its consumer and destroyed by a routine pod
recycle.

This module publishes that directory to object storage BEFORE the completion
callback is delivered, so the evidence is durable before anything is told the
crawl finished. If the callback is then lost, qe-central's recovery path can
still find the evidence — a property the pod-local design could not offer.

WHY THIS DOES NOT IMPORT ``nexus_sdk.storage``
==============================================
qe-central's consumer half DOES use the SDK's ``ArtifactStore`` (see
``platform/qe-central/app/storage/object_store.py``), which is the house
convention. This service cannot: the contained explorer deliberately does not
depend on ``nexus_sdk``. It is not in ``requirements.txt``, and the one place
the engine touches the SDK (``emit.py``'s PII detector) is an import-GUARDED
optional with a local fallback. The container is a quarantined Playwright image
carrying only fastapi / httpx / pydantic, and that minimalism is a security
property of the service that runs a browser against customer applications — not
an oversight to be corrected by adding a dependency.

So the producer mirrors the consumer's contract BY HAND, and a contract test
(``tests/fleet/test_t_fl_03_object_storage_handoff.py``) asserts the two agree
by comparing this module's output against the real
``ArtifactStore.build_key``. If they ever drift, the explorer publishes where
qe-central will never look — a total, silent evidence loss with no error on
either side — so that test is the thing standing between this design and that
outcome.

THE CONTRACT
============
Environment: the HOUSE contract (``NEXUS_STORAGE_BACKEND`` + ``S3_*``), the same
variables platform-api, the engines and qe-central already read, so one
deployment configures every service.

Key layout, mirroring ``ArtifactStore.build_key(tenant, "eyes", "crawls",
crawl_id)``::

    {tenant_id}/eyes/crawls/{crawl_id}/{path relative to the crawl dir}

The tenant segment is load-bearing: it is what stops a misconfigured worker
writing across a tenant boundary, and it is why the key is built from the
crawl's OWNING tenant rather than from the crawl id alone.

FAIL-SOFT, AND LOUD. A publish failure NEVER aborts the crawl or the callback:
the crawl really did happen, the local record really was written, and refusing to
report it would turn a storage incident into a lost crawl. The failure is logged
at ERROR and returned so the caller can put it on the callback body, where an
operator will see it.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# ── The HOUSE env contract (nexus_sdk.storage.base.StorageConfig) ───────────
ENV_BACKEND = "NEXUS_STORAGE_BACKEND"
ENV_S3_ENDPOINT = "S3_ENDPOINT"
ENV_S3_REGION = "S3_REGION"
ENV_S3_ACCESS_KEY = "S3_ACCESS_KEY"
ENV_S3_SECRET_KEY = "S3_SECRET_KEY"
ENV_S3_BUCKET = "S3_BUCKET"

#: MUST MATCH platform/qe-central/app/storage/object_store.py.
MANIFEST_FILENAME = "manifest.jsonl"
EVIDENCE_NAMESPACE = "eyes"
EVIDENCE_SEGMENT = "crawls"

#: Backends whose storage is genuinely remote. Anything else (``local``, unset)
#: makes publishing a no-op — today's shared-volume behaviour, unchanged.
_REMOTE_BACKENDS = frozenset({"s3", "gcs", "azure"})


def backend() -> str:
    return (os.environ.get(ENV_BACKEND, "") or "local").strip().lower()


def is_object_backed() -> bool:
    return backend() in _REMOTE_BACKENDS


def _safe_segment(segment: str) -> str:
    """Reject traversal and absolute components.

    A hand-copy of ``nexus_sdk.storage.artifact_store._safe_segment`` — same
    rejections, so a crawl id or tenant id that the consumer would refuse is
    refused here too rather than producing a key the consumer cannot read.
    """
    s = (segment or "").strip().strip("/").replace("\\", "/")
    if not s or s.startswith(".") or ".." in s.split("/"):
        raise ValueError(f"invalid storage key segment: {segment!r}")
    return s


def evidence_prefix(tenant_id: str, crawl_id: str) -> str:
    """``{tenant}/eyes/crawls/{crawl_id}`` — mirrors ArtifactStore.build_key."""
    return "/".join((
        _safe_segment(tenant_id), EVIDENCE_NAMESPACE,
        _safe_segment(EVIDENCE_SEGMENT), _safe_segment(crawl_id),
    ))


def object_key(tenant_id: str, crawl_id: str, relative: str) -> str:
    """The key for one file of one crawl."""
    rel = str(relative).replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ValueError(f"unsafe relative path in evidence key: {relative!r}")
    return f"{evidence_prefix(tenant_id, crawl_id)}/{rel}"


def _client():
    import boto3
    from botocore.config import Config

    kwargs = {"config": Config(retries={"max_attempts": 3, "mode": "standard"})}
    endpoint = (os.environ.get(ENV_S3_ENDPOINT, "") or "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    region = (os.environ.get(ENV_S3_REGION, "") or "").strip()
    if region:
        kwargs["region_name"] = region
    access = (os.environ.get(ENV_S3_ACCESS_KEY, "") or "").strip()
    secret = (os.environ.get(ENV_S3_SECRET_KEY, "") or "").strip()
    if access and secret:
        kwargs["aws_access_key_id"] = access
        kwargs["aws_secret_access_key"] = secret
    return boto3.client("s3", **kwargs)


def publish_crawl_evidence(work_dir: str, crawl_id: str,
                           tenant_id: str) -> dict:
    """Publish ``{work_dir}/{crawl_id}/`` to object storage.

    Returns ``{"published": bool, "files": int, "error": str}`` — never raises.

    THE MANIFEST IS UPLOADED LAST. Its presence is what the consumer treats as
    "this crawl's evidence is here", so publishing it before the frames it
    references would let a consumer that raced the upload ingest a manifest whose
    screenshots are still missing.
    """
    if not is_object_backed():
        return {"published": False, "files": 0, "error": ""}

    if not str(tenant_id or "").strip():
        # Without the owning tenant there is no tenant-scoped key to write, and
        # inventing one would put a crawl's evidence outside its tenant's
        # prefix — the isolation property the key layout exists to provide.
        msg = "no tenant_id on the crawl — refusing to publish un-scoped evidence"
        logger.error("qec.explorer.evidence_publish_unscoped crawl_id=%s %s",
                     crawl_id, msg)
        return {"published": False, "files": 0, "error": msg}

    root = Path(work_dir) / crawl_id
    if not root.is_dir():
        return {"published": False, "files": 0,
                "error": f"no crawl directory at {root}"}

    bucket = (os.environ.get(ENV_S3_BUCKET, "") or "").strip()
    if not bucket:
        msg = f"{ENV_BACKEND}={backend()} but {ENV_S3_BUCKET} is unset"
        logger.error("qec.explorer.evidence_publish_misconfigured crawl_id=%s %s",
                     crawl_id, msg)
        return {"published": False, "files": 0, "error": msg}

    try:
        client = _client()
        uploaded = 0
        manifest: Path | None = None
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.name == MANIFEST_FILENAME and path.parent == root:
                manifest = path
                continue
            client.upload_file(
                str(path), bucket,
                object_key(tenant_id, crawl_id,
                           path.relative_to(root).as_posix()))
            uploaded += 1
        if manifest is not None:
            client.upload_file(
                str(manifest), bucket,
                object_key(tenant_id, crawl_id, MANIFEST_FILENAME))
            uploaded += 1
        logger.warning(
            "qec.explorer.evidence_published crawl_id=%s files=%d bucket=%s",
            crawl_id, uploaded, bucket)
        return {"published": True, "files": uploaded, "error": ""}
    except Exception as exc:
        # NEVER abort the callback: the crawl happened, the local record was
        # written, and refusing to report it would turn a storage incident into
        # a lost crawl. Loud, and carried on the callback body.
        logger.error(
            "qec.explorer.evidence_publish_failed crawl_id=%s error=%s — the "
            "evidence is only on this pod's local disk and will be lost if the "
            "pod is replaced", crawl_id, str(exc)[:300])
        return {"published": False, "files": 0, "error": str(exc)[:300]}


__all__ = ["ENV_BACKEND", "ENV_S3_BUCKET", "ENV_S3_ENDPOINT", "ENV_S3_REGION",
           "ENV_S3_ACCESS_KEY", "ENV_S3_SECRET_KEY", "EVIDENCE_NAMESPACE",
           "EVIDENCE_SEGMENT", "MANIFEST_FILENAME", "backend",
           "evidence_prefix", "is_object_backed", "object_key",
           "publish_crawl_evidence"]
