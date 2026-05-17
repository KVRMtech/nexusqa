"""
Nexus SDK — Storage Base Interface.

Abstract base class that all storage backends implement.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional


@dataclass
class StorageConfig:
    """Configuration for object storage."""

    # Backend selector. Auto-detected from env if blank:
    #   "s3" | "azure" | "gcs" | "local"
    backend: str = field(
        default_factory=lambda: os.environ.get("NEXUS_STORAGE_BACKEND", "")
    )

    # S3-compatible settings (AWS, MinIO, GCS interop, etc.)
    s3_endpoint: str = field(
        default_factory=lambda: os.environ.get("S3_ENDPOINT", "")
    )
    s3_region: str = field(
        default_factory=lambda: os.environ.get("S3_REGION", "us-east-1")
    )
    s3_access_key: str = field(
        default_factory=lambda: os.environ.get("S3_ACCESS_KEY", "")
    )
    s3_secret_key: str = field(
        default_factory=lambda: os.environ.get("S3_SECRET_KEY", "")
    )
    s3_bucket: str = field(
        default_factory=lambda: os.environ.get("S3_BUCKET", "nexus-artifacts")
    )
    s3_use_ssl: bool = field(
        default_factory=lambda: os.environ.get("S3_USE_SSL", "true").lower() == "true"
    )

    # Local filesystem fallback (dev mode)
    local_root: str = field(
        default_factory=lambda: os.environ.get("NEXUS_STORAGE_PATH", "/data/nexus")
    )

    # Presigned URL expiry (seconds)
    presign_expiry: int = field(
        default_factory=lambda: int(os.environ.get("S3_PRESIGN_EXPIRY", "3600"))
    )

    def resolve_backend(self) -> str:
        """Return the active backend name based on explicit selector and env."""
        explicit = (self.backend or "").lower().strip()
        if explicit in {"s3", "azure", "gcs", "local"}:
            return explicit
        if os.environ.get("AZURE_STORAGE_ACCOUNT"):
            return "azure"
        if os.environ.get("GCS_BUCKET"):
            return "gcs"
        if self.s3_endpoint or os.environ.get("AWS_REGION"):
            return "s3"
        return "local"


class StorageBackend(ABC):
    """Abstract storage interface implemented by S3 and local backends."""

    @abstractmethod
    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """
        Upload an object.

        Args:
            key: Object key (e.g. "tenant-123/audio/session-1.wav")
            data: Raw bytes to upload
            content_type: MIME type
            metadata: Optional user metadata dict

        Returns:
            The stored object key.
        """
        ...

    @abstractmethod
    async def upload_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Upload from an async byte stream (for large files)."""
        ...

    @abstractmethod
    async def download(self, key: str) -> bytes:
        """Download an object as bytes."""
        ...

    @abstractmethod
    async def download_stream(self, key: str) -> AsyncIterator[bytes]:
        """Download an object as an async byte stream."""
        ...

    @abstractmethod
    async def delete(self, key: str) -> bool:
        """Delete an object. Returns True if deleted."""
        ...

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Check if an object exists."""
        ...

    @abstractmethod
    async def presign(self, key: str, expiry: Optional[int] = None) -> str:
        """
        Generate a presigned download URL.

        Args:
            key: Object key
            expiry: URL expiry in seconds (default from config)

        Returns:
            Presigned URL string.
        """
        ...

    @abstractmethod
    async def list_objects(
        self, prefix: str, max_keys: int = 1000
    ) -> list[str]:
        """List object keys under a prefix."""
        ...

    @abstractmethod
    async def head(self, key: str) -> Optional[dict]:
        """
        Get object metadata without downloading.

        Returns dict with content_length, content_type, last_modified, metadata.
        Returns None if object doesn't exist.
        """
        ...
