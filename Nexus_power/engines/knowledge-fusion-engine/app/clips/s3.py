"""S3 storage for clips.

Wraps boto3 ``put_object`` and ``generate_presigned_url`` behind a
small async-friendly facade. boto3 itself is sync; we run calls in
the default executor so they don't block the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class S3StorageConfig:
    bucket: str
    region: Optional[str] = None
    prefix: str = "clips/"
    sse_kms_key_id: Optional[str] = None
    signed_url_ttl_seconds: int = 3600
    public_acl: bool = False

    def s3_key(
        self,
        *,
        tenant_id: str,
        session_id: str,
        clip_id: str,
        extension: str,
    ) -> str:
        ext = extension.lstrip(".")
        prefix = self.prefix.rstrip("/")
        return f"{prefix}/{tenant_id}/{session_id}/{clip_id}.{ext}"

    def thumbnail_key(
        self,
        *,
        tenant_id: str,
        session_id: str,
        clip_id: str,
    ) -> str:
        prefix = self.prefix.rstrip("/")
        return f"{prefix}/{tenant_id}/{session_id}/{clip_id}.jpg"


class S3ClipStorage:
    """Async wrapper around the boto3 S3 client.

    Initialised once per process. The boto3 client is thread-safe;
    multiple coroutines may invoke this concurrently.
    """

    def __init__(self, config: S3StorageConfig):
        try:
            import boto3  # noqa: F401  (deferred import)
            from botocore.config import Config as BotoConfig  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "boto3 is required for S3ClipStorage"
            ) from exc
        import boto3
        from botocore.config import Config as BotoConfig

        self._cfg = config
        client_kwargs: dict = {}
        if config.region:
            client_kwargs["region_name"] = config.region
        client_kwargs["config"] = BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
        )
        self._client = boto3.client("s3", **client_kwargs)

    @property
    def config(self) -> S3StorageConfig:
        return self._cfg

    async def upload(
        self,
        *,
        local_path: str,
        s3_key: str,
        content_type: str,
    ) -> None:
        extra_args: dict = {"ContentType": content_type}
        if self._cfg.sse_kms_key_id:
            extra_args["ServerSideEncryption"] = "aws:kms"
            extra_args["SSEKMSKeyId"] = self._cfg.sse_kms_key_id
        elif not self._cfg.public_acl:
            extra_args["ServerSideEncryption"] = "AES256"

        def _put() -> None:
            self._client.upload_file(
                Filename=local_path,
                Bucket=self._cfg.bucket,
                Key=s3_key,
                ExtraArgs=extra_args,
            )

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _put)

    async def presigned_url(
        self,
        *,
        s3_key: str,
        ttl_seconds: Optional[int] = None,
    ) -> str:
        ttl = ttl_seconds or self._cfg.signed_url_ttl_seconds

        def _sign() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._cfg.bucket, "Key": s3_key},
                ExpiresIn=ttl,
            )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _sign)

    async def head(self, *, s3_key: str) -> Optional[dict]:
        """Stat an object. Returns None when the key is absent."""

        def _head() -> Optional[dict]:
            try:
                return self._client.head_object(
                    Bucket=self._cfg.bucket, Key=s3_key
                )
            except Exception as exc:
                # ClientError(NoSuchKey/404) → return None;
                # everything else propagates.
                code = getattr(getattr(exc, "response", {}).get("Error", {}), "get", lambda *_: None)("Code")
                if code in ("404", "NoSuchKey", "NotFound"):
                    return None
                raise

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _head)
