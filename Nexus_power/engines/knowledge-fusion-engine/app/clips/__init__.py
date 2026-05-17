"""Media clip service — ffmpeg cuts + S3-backed delivery + idempotent cache.

Public surface:
    * ``ClipService``               — orchestrates resolver + ffmpeg + S3 + DB cache.
    * ``ClipRequest`` / ``Clip``    — DTOs.
    * ``MediaResolver``             — abstract source-media resolver.
    * ``CanonicalArtifactResolver`` — default implementation that reads
                                      ``full_artifact_json.source_media_uri``
                                      / ``full_artifact_json.media_uri``.
    * ``S3ClipStorage``             — boto3-backed storage with SSE-KMS.
"""

from __future__ import annotations

from .service import (
    Clip,
    ClipError,
    ClipNotResolvable,
    ClipRequest,
    ClipService,
    MediaResolver,
    CanonicalArtifactResolver,
)
from .s3 import S3ClipStorage, S3StorageConfig

__all__ = [
    "Clip",
    "ClipError",
    "ClipNotResolvable",
    "ClipRequest",
    "ClipService",
    "MediaResolver",
    "CanonicalArtifactResolver",
    "S3ClipStorage",
    "S3StorageConfig",
]
