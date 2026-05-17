"""
Nexus SDK — Local Filesystem Storage Backend.

Development fallback when S3 is not configured.
Stores files under NEXUS_STORAGE_PATH with the same key structure.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator, Optional

from nexus_sdk.storage.base import StorageBackend, StorageConfig

logger = logging.getLogger(__name__)


class LocalStorage(StorageBackend):
    """Filesystem-based storage for development."""

    def __init__(self, config: StorageConfig):
        self._root = Path(config.local_root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Sanitize key to prevent path traversal
        safe_key = key.lstrip("/").replace("..", "")
        return self._root / safe_key

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, data)
        logger.debug("Local: stored %s (%d bytes)", key, len(data))
        return key

    async def upload_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        await asyncio.to_thread(path.write_bytes, b"".join(chunks))
        return key

    async def download(self, key: str) -> bytes:
        path = self._path(key)
        if not path.exists():
            raise FileNotFoundError(f"Object not found: {key}")
        return await asyncio.to_thread(path.read_bytes)

    async def download_stream(self, key: str) -> AsyncIterator[bytes]:
        data = await self.download(key)
        chunk_size = 1024 * 1024
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    async def delete(self, key: str) -> bool:
        path = self._path(key)
        try:
            if path.exists():
                await asyncio.to_thread(path.unlink)
                return True
            return False
        except Exception as e:
            logger.error("Local: delete failed for %s: %s", key, e)
            return False

    async def exists(self, key: str) -> bool:
        return self._path(key).exists()

    async def presign(self, key: str, expiry: Optional[int] = None) -> str:
        # Local storage returns a file:// URI
        return f"file://{self._path(key)}"

    async def list_objects(
        self, prefix: str, max_keys: int = 1000
    ) -> list[str]:
        root = self._path(prefix)
        if not root.exists():
            return []
        keys = []
        for path in sorted(root.rglob("*")):
            if path.is_file():
                rel = path.relative_to(self._root)
                keys.append(str(rel).replace("\\", "/"))
                if len(keys) >= max_keys:
                    break
        return keys

    async def head(self, key: str) -> Optional[dict]:
        path = self._path(key)
        if not path.exists():
            return None
        stat = path.stat()
        return {
            "content_length": stat.st_size,
            "content_type": "application/octet-stream",
            "last_modified": stat.st_mtime,
            "metadata": {},
            "etag": "",
        }
