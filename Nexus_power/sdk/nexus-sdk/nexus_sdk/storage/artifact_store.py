"""
Nexus SDK — Artifact Store helper.

Higher-level helpers on top of StorageBackend that engines use to:

  - Build canonical object keys (tenant_id / session_id / namespace / ...).
  - Upload a local file (streamed) and return its key.
  - Upload an entire directory tree under a key prefix.
  - Resolve a key to either a presigned URL (cloud backends) or a
    same-engine proxy path (local backend during dev).

The store enforces tenant-scoped prefixes so a misconfigured engine
cannot write across tenant boundaries.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Optional

from nexus_sdk.storage.base import StorageBackend, StorageConfig
from nexus_sdk.storage.local import LocalStorage

logger = logging.getLogger(__name__)


_DEFAULT_CHUNK = 4 * 1024 * 1024  # 4 MiB

# Per-engine canonical namespaces. Engines pass `namespace=` so keys are
# globally readable: "<tenant>/<engine>/<session>/<...>".
_VALID_NAMESPACES = {
    "ears",
    "eyes",
    "legs",
    "spine",
    "mouth",
    "backbone",
    "brain",
    "heart",
    "shield",
    "nerves",
    "hands",
}


def _safe_segment(segment: str) -> str:
    """Reject path traversal and absolute components."""
    s = (segment or "").strip().strip("/").replace("\\", "/")
    if not s or s.startswith(".") or ".." in s.split("/"):
        raise ValueError(f"invalid storage key segment: {segment!r}")
    return s


@dataclass
class ArtifactRef:
    """A pointer to a stored artifact returned from upload helpers."""

    key: str          # canonical storage key (used in DB columns)
    size: int         # bytes uploaded
    content_type: str # detected MIME type
    backend: str      # "s3" | "azure" | "gcs" | "local"


class ArtifactStore:
    """Engine-facing helper over a StorageBackend."""

    def __init__(
        self,
        backend: StorageBackend,
        config: Optional[StorageConfig] = None,
    ):
        self._backend = backend
        self._config = config or StorageConfig()

    @property
    def backend(self) -> StorageBackend:
        return self._backend

    @property
    def backend_name(self) -> str:
        return self._config.resolve_backend()

    @property
    def is_local(self) -> bool:
        return isinstance(self._backend, LocalStorage)

    # ── Key construction ────────────────────────────────────────

    def build_key(
        self,
        tenant_id: str,
        namespace: str,
        *segments: str,
    ) -> str:
        """Return a canonical key '<tenant>/<namespace>/<segments...>'."""
        ns = namespace.lower().strip()
        if ns not in _VALID_NAMESPACES:
            raise ValueError(
                f"unknown artifact namespace {namespace!r}; expected one of "
                f"{sorted(_VALID_NAMESPACES)}"
            )
        parts = [_safe_segment(tenant_id), ns]
        parts.extend(_safe_segment(s) for s in segments if s)
        return "/".join(parts)

    # ── Uploads ────────────────────────────────────────────────

    async def upload_bytes(
        self,
        key: str,
        data: bytes,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
    ) -> ArtifactRef:
        ct = content_type or "application/octet-stream"
        await self._backend.upload(
            key, data, content_type=ct, metadata=metadata
        )
        return ArtifactRef(
            key=key, size=len(data), content_type=ct, backend=self.backend_name
        )

    async def upload_file(
        self,
        key: str,
        path: str | Path,
        content_type: Optional[str] = None,
        metadata: Optional[dict[str, str]] = None,
        chunk_size: int = _DEFAULT_CHUNK,
    ) -> ArtifactRef:
        """Stream-upload a local file. Caller may delete the file after."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(p)

        guessed, _ = mimetypes.guess_type(str(p))
        ct = content_type or guessed or "application/octet-stream"
        size = p.stat().st_size

        async def _stream() -> AsyncIterator[bytes]:
            loop = asyncio.get_running_loop()
            fh = await loop.run_in_executor(None, p.open, "rb")
            try:
                while True:
                    chunk = await loop.run_in_executor(
                        None, fh.read, chunk_size
                    )
                    if not chunk:
                        break
                    yield chunk
            finally:
                await loop.run_in_executor(None, fh.close)

        await self._backend.upload_stream(
            key, _stream(), content_type=ct, metadata=metadata
        )
        return ArtifactRef(
            key=key, size=size, content_type=ct, backend=self.backend_name
        )

    async def upload_directory(
        self,
        key_prefix: str,
        directory: str | Path,
        include_suffixes: Optional[set[str]] = None,
        metadata: Optional[dict[str, str]] = None,
        concurrency: int = 8,
    ) -> list[ArtifactRef]:
        """
        Upload every file under `directory` to `<key_prefix>/<relpath>`.
        Returns the list of refs in deterministic (sorted) order.
        """
        root = Path(directory)
        if not root.is_dir():
            return []

        files: list[Path] = []
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            if include_suffixes and f.suffix.lower() not in include_suffixes:
                continue
            files.append(f)

        sem = asyncio.Semaphore(max(1, concurrency))
        refs: list[ArtifactRef] = []

        async def _one(f: Path) -> ArtifactRef:
            rel = str(f.relative_to(root)).replace("\\", "/")
            key = f"{key_prefix}/{rel}"
            async with sem:
                return await self.upload_file(key, f, metadata=metadata)

        results = await asyncio.gather(*(_one(f) for f in files))
        refs.extend(results)
        return refs

    # ── Reads / URLs ───────────────────────────────────────────

    async def download_bytes(self, key: str) -> bytes:
        return await self._backend.download(key)

    async def stream(self, key: str) -> AsyncIterator[bytes]:
        async for chunk in self._backend.download_stream(key):
            yield chunk

    async def presign(
        self, key: str, expiry: Optional[int] = None
    ) -> str:
        return await self._backend.presign(key, expiry=expiry)

    async def asset_url(
        self,
        key: str,
        proxy_base: Optional[str] = None,
        expiry: Optional[int] = None,
    ) -> str:
        """
        Resolve a key to a URL the client can fetch:
          - Cloud backends → presigned URL (CDN-aware downstream).
          - Local backend  → '<proxy_base>/<key>' so the engine itself
            can stream the file. Caller must implement the proxy route.
        """
        if self.is_local and proxy_base:
            return f"{proxy_base.rstrip('/')}/{key.lstrip('/')}"
        return await self.presign(key, expiry=expiry)

    async def exists(self, key: str) -> bool:
        return await self._backend.exists(key)

    async def delete(self, key: str) -> bool:
        return await self._backend.delete(key)

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every key under a prefix. Returns count of deleted keys."""
        keys = await self._backend.list_objects(prefix, max_keys=10_000)
        count = 0
        for k in keys:
            if await self._backend.delete(k):
                count += 1
        return count


# Module-level singleton convenience — engines that prefer DI can ignore.
_default_store: Optional[ArtifactStore] = None


def get_default_artifact_store() -> ArtifactStore:
    """Return a process-wide default store backed by env-derived config."""
    global _default_store
    if _default_store is None:
        from nexus_sdk.storage import create_storage

        cfg = StorageConfig()
        _default_store = ArtifactStore(create_storage(cfg), cfg)
    return _default_store
