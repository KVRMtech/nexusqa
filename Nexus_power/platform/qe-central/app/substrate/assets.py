"""QE-Central — screenshot assets via the eyes ArtifactStore namespace (R-5).

Uploads crawl screenshots exactly the way the eyes engine stores frames so
the WHOLE existing serving chain works unchanged:

  * durable copy → ``ArtifactStore.build_key(tenant, 'eyes', session,
    '{crawl_id}_frames', 'frame_NNNNN.png')`` (upload pattern per
    eyes-engine/main.py:948-1000);
  * the DB-facing ``frame_asset_path`` is the eyes serving-route shape
    ``{tenant}/{session}/{crawl_id}_frames/frame_NNNNN.png`` — the eyes GET
    route re-derives the storage key from exactly those segments
    (eyes-engine/main.py:1061-1129) and platform-api guards it with
    ``safe_frame_asset_path`` (database.py:147-188), which we assert HERE
    at write time so an unservable path can never be persisted;
  * a best-effort working copy lands on the local frame-storage volume
    (``NEXUS_FRAME_STORAGE_PATH``) mirroring eyes' local-dev fast path —
    its failure is logged, never fatal (the ArtifactStore copy is the
    durable source of truth).
"""
from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from ..config import settings
from ..db import safe_frame_asset_path
from .schema import CRAWL_ID_PATTERN

logger = logging.getLogger(__name__)

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_store_cache = None


def _store():
    """Lazily build the env-configured SDK ArtifactStore (cached).

    Config comes from the SAME env contract platform-api/eyes use
    (``NEXUS_STORAGE_BACKEND`` + backend-specific vars) so assets are
    co-readable across services — design §3.1 hard requirement.
    """
    global _store_cache
    if _store_cache is None:
        from nexus_sdk.storage import create_storage
        from nexus_sdk.storage.artifact_store import ArtifactStore
        from nexus_sdk.storage.base import StorageConfig

        cfg = StorageConfig(backend=settings.nexus_storage_backend)
        _store_cache = ArtifactStore(create_storage(cfg), cfg)
    return _store_cache


def reset_store_cache_for_tests() -> None:
    """Drop the cached store so tests can re-point storage env vars."""
    global _store_cache
    _store_cache = None


class FrameRef(BaseModel):
    """One stored screenshot: DB path + storage key + provenance."""

    frame_index: int
    timestamp_seconds: float
    frame_asset_path: str
    storage_key: str
    size_bytes: int


def frame_file_name(frame_index: int) -> str:
    """Canonical frame file name (``frame_NNNNN.png`` — eyes convention)."""
    if frame_index < 0:
        raise ValueError(f"frame_index must be >= 0, got {frame_index}")
    return f"frame_{frame_index:05d}.png"


def frame_asset_rel_path(
    tenant_id: str, session_id: str, crawl_id: str, frame_index: int,
) -> str:
    """The ``visual_frames.frame_asset_path`` value for one screenshot.

    Shape pinned by the platform-api serving guard:
    ``{tenant}/{session}/{job_id}_frames/frame_NNNNN.png`` with
    ``job_id = crawl_id`` (database.py:147-158).
    """
    return f"{tenant_id}/{session_id}/{crawl_id}_frames/{frame_file_name(frame_index)}"


async def store_screenshots(
    tenant_id: str,
    session_id: str,
    crawl_id: str,
    files: list[tuple[int, bytes, float]],
) -> list[FrameRef]:
    """Upload crawl screenshots and return their validated frame refs.

    Args:
        tenant_id:  owning tenant (becomes the key prefix — tenant scoped).
        session_id: the crawl's ``sessions`` row id.
        crawl_id:   the crawl id (job segment ``{crawl_id}_frames``).
        files:      ``[(frame_index, png_bytes, timestamp_seconds), ...]``.

    Every emitted ``frame_asset_path`` is asserted against
    ``safe_frame_asset_path`` before it is returned — a path the serving
    side would refuse is a hard error HERE, never a broken row later.
    Raises ``ValueError`` on invalid input, ``RuntimeError`` on storage
    failure (loud — a lost screenshot must never be silent).
    """
    if not tenant_id or not session_id:
        raise ValueError("tenant_id and session_id are required")
    if not CRAWL_ID_PATTERN.match(crawl_id or ""):
        raise ValueError(f"invalid crawl_id {crawl_id!r} for storage key segments")

    store = _store()
    refs: list[FrameRef] = []
    for frame_index, data, timestamp_seconds in sorted(files, key=lambda f: f[0]):
        if not data or not bytes(data).startswith(_PNG_MAGIC):
            raise ValueError(f"screenshot frame_index={frame_index} is not a PNG")
        if timestamp_seconds < 0:
            raise ValueError(
                f"screenshot frame_index={frame_index} has negative timestamp"
            )
        name = frame_file_name(frame_index)
        key = store.build_key(
            tenant_id, "eyes", session_id, f"{crawl_id}_frames", name,
        )
        try:
            ref = await store.upload_bytes(key, bytes(data), content_type="image/png")
        except Exception as exc:
            logger.error(
                "qec.assets.upload_failed",
                extra={"key": key, "frame_index": frame_index,
                       "error": str(exc)[:300]},
            )
            raise RuntimeError(
                f"screenshot upload failed for frame {frame_index}: {exc}"
            ) from exc

        rel_path = frame_asset_rel_path(tenant_id, session_id, crawl_id, frame_index)
        if safe_frame_asset_path(tenant_id, rel_path) != rel_path:
            raise RuntimeError(
                f"emitted frame path {rel_path!r} fails the serving-side "
                f"safe_frame_asset_path guard — refusing to persist an "
                f"unservable screenshot reference"
            )

        # Best-effort working copy on the frame-storage volume (eyes' local
        # fast path). The ArtifactStore copy above is the durable one.
        try:
            working = Path(settings.nexus_frame_storage_path) / rel_path
            working.parent.mkdir(parents=True, exist_ok=True)
            working.write_bytes(bytes(data))
        except Exception as exc:
            logger.warning(
                "qec.assets.working_copy_failed",
                extra={"path": rel_path, "error": str(exc)[:300]},
            )

        refs.append(FrameRef(
            frame_index=frame_index,
            timestamp_seconds=float(timestamp_seconds),
            frame_asset_path=rel_path,
            storage_key=ref.key,
            size_bytes=ref.size,
        ))

    logger.info(
        "qec.assets.screenshots_stored",
        extra={"tenant_id": tenant_id, "session_id": session_id,
               "crawl_id": crawl_id, "count": len(refs)},
    )
    return refs
