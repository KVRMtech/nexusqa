"""
Nexus SDK — Cloud Object Storage.

Provides a unified interface for file storage across environments:
  - S3Storage:        Production (AWS S3, MinIO, any S3-compatible)
  - AzureBlobStorage: Production (Azure Blob)
  - GCSStorage:       Production (Google Cloud Storage)
  - LocalStorage:     Development (filesystem fallback)

Backend selection (in order):
  1. StorageConfig.backend or NEXUS_STORAGE_BACKEND env (explicit)
  2. AZURE_STORAGE_ACCOUNT set → azure
  3. GCS_BUCKET set           → gcs
  4. S3_ENDPOINT or AWS_REGION → s3
  5. fallback                  → local

Usage:
    from nexus_sdk.storage import create_storage

    storage = create_storage()   # auto-selects backend from env
    await storage.upload("tenant-123/audio/session-1.wav", data)
    url = await storage.presign("tenant-123/audio/session-1.wav")
    data = await storage.download("tenant-123/audio/session-1.wav")
"""

from __future__ import annotations

import logging

from nexus_sdk.storage.base import StorageBackend, StorageConfig
from nexus_sdk.storage.s3 import S3Storage
from nexus_sdk.storage.local import LocalStorage
from nexus_sdk.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


def create_storage(config: StorageConfig | None = None) -> StorageBackend:
    """
    Factory: create the appropriate storage backend.

    Reads NEXUS_STORAGE_BACKEND or infers from env (see module docstring).
    """
    cfg = config or StorageConfig()
    backend = cfg.resolve_backend()

    if backend == "azure":
        from nexus_sdk.storage.azure import AzureBlobStorage

        return AzureBlobStorage(cfg)
    if backend == "gcs":
        from nexus_sdk.storage.gcs import GCSStorage

        return GCSStorage(cfg)
    if backend == "s3":
        return S3Storage(cfg)
    return LocalStorage(cfg)


__all__ = [
    "StorageBackend",
    "StorageConfig",
    "S3Storage",
    "LocalStorage",
    "ArtifactStore",
    "create_storage",
]
