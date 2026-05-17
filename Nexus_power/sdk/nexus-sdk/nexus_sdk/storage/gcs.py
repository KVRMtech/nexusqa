"""
Nexus SDK — Google Cloud Storage Backend.

Uses gcloud-aio-storage for native async I/O.

Required environment:
    GCS_PROJECT                — GCP project id (optional; inferred from ADC)
    GCS_BUCKET                 — bucket name (falls back to S3_BUCKET)
    GCS_SERVICE_ACCOUNT_FILE   — path to JSON key (optional under workload identity)

When running with Workload Identity in GKE or with an attached service account,
no credentials env is needed; ADC handles it.
"""

from __future__ import annotations

import io
import logging
import os
from typing import AsyncIterator, Optional

from nexus_sdk.storage.base import StorageBackend, StorageConfig

logger = logging.getLogger(__name__)


class GCSStorage(StorageBackend):
    """Google Cloud Storage backend using gcloud-aio-storage."""

    def __init__(self, config: StorageConfig):
        self._config = config
        self._bucket_name = os.environ.get("GCS_BUCKET", config.s3_bucket)
        self._project = os.environ.get("GCS_PROJECT", "") or None
        self._sa_file = os.environ.get("GCS_SERVICE_ACCOUNT_FILE", "")
        self._client = None

    async def _get_client(self):
        if self._client is not None:
            return self._client
        from gcloud.aio.storage import Storage  # type: ignore

        kwargs = {}
        if self._sa_file:
            kwargs["service_file"] = self._sa_file
        self._client = Storage(**kwargs)
        return self._client

    async def close(self):
        if self._client:
            await self._client.close()
            self._client = None

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        client = await self._get_client()
        await client.upload(
            bucket=self._bucket_name,
            object_name=key,
            file_data=data,
            content_type=content_type,
            metadata={"metadata": metadata} if metadata else None,
        )
        logger.debug("GCS: uploaded %s (%d bytes)", key, len(data))
        return key

    async def upload_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        # gcloud-aio's resumable upload uses content-length; we buffer.
        # For very large objects callers should set GCS_RESUMABLE_THRESHOLD
        # via the SDK config in future iterations.
        buf = bytearray()
        async for chunk in stream:
            buf.extend(chunk)
        return await self.upload(key, bytes(buf), content_type, metadata)

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        return await client.download(
            bucket=self._bucket_name, object_name=key
        )

    async def download_stream(self, key: str) -> AsyncIterator[bytes]:
        data = await self.download(key)
        chunk_size = 1024 * 1024
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]

    async def delete(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.delete(
                bucket=self._bucket_name, object_name=key
            )
            return True
        except Exception as e:
            logger.error("GCS: delete failed for %s: %s", key, e)
            return False

    async def exists(self, key: str) -> bool:
        try:
            head = await self.head(key)
            return head is not None
        except Exception:
            return False

    async def presign(self, key: str, expiry: Optional[int] = None) -> str:
        client = await self._get_client()
        ttl = expiry or self._config.presign_expiry
        return await client.get_signed_url(
            bucket=self._bucket_name,
            object_name=key,
            expiration=ttl,
            http_method="GET",
        )

    async def list_objects(
        self, prefix: str, max_keys: int = 1000
    ) -> list[str]:
        client = await self._get_client()
        params = {"prefix": prefix, "maxResults": str(min(max_keys, 1000))}
        keys: list[str] = []
        page_token: Optional[str] = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            resp = await client.list_objects(self._bucket_name, params=params)
            for item in resp.get("items", []) or []:
                keys.append(item["name"])
                if len(keys) >= max_keys:
                    return keys
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return keys

    async def head(self, key: str) -> Optional[dict]:
        client = await self._get_client()
        try:
            meta = await client.download_metadata(
                bucket=self._bucket_name, object_name=key
            )
            return {
                "content_length": int(meta.get("size", 0)),
                "content_type": meta.get("contentType", ""),
                "last_modified": meta.get("updated", ""),
                "metadata": meta.get("metadata", {}) or {},
                "etag": meta.get("etag", ""),
            }
        except Exception:
            return None
