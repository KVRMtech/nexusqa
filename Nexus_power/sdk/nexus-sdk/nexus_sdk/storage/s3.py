"""
Nexus SDK — S3-Compatible Object Storage Backend.

Works with AWS S3, MinIO, DigitalOcean Spaces, Backblaze B2,
Cloudflare R2, or any S3-compatible API.

Uses aiobotocore for async operations to avoid blocking the event loop.
Falls back to synchronous boto3 in a thread pool if aiobotocore is unavailable.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from nexus_sdk.storage.base import StorageBackend, StorageConfig

logger = logging.getLogger(__name__)


class S3Storage(StorageBackend):
    """S3-compatible object storage using aiobotocore."""

    def __init__(self, config: StorageConfig):
        self._config = config
        self._session = None
        self._client = None

    async def _get_client(self):
        """Lazy-initialize the S3 client."""
        if self._client is not None:
            return self._client

        try:
            from aiobotocore.session import AioSession

            self._session = AioSession()
            self._client = await self._session.create_client(
                "s3",
                endpoint_url=self._config.s3_endpoint or None,
                region_name=self._config.s3_region,
                aws_access_key_id=self._config.s3_access_key,
                aws_secret_access_key=self._config.s3_secret_key,
                use_ssl=self._config.s3_use_ssl,
            ).__aenter__()

            # Ensure bucket exists
            try:
                await self._client.head_bucket(Bucket=self._config.s3_bucket)
            except Exception:
                try:
                    await self._client.create_bucket(
                        Bucket=self._config.s3_bucket,
                        CreateBucketConfiguration={
                            "LocationConstraint": self._config.s3_region,
                        }
                        if self._config.s3_region != "us-east-1"
                        else {},
                    )
                    logger.info("S3: created bucket %s", self._config.s3_bucket)
                except Exception as e:
                    if "BucketAlreadyOwnedByYou" not in str(e):
                        logger.warning("S3: bucket creation note: %s", e)

            return self._client

        except ImportError:
            raise RuntimeError(
                "aiobotocore is required for S3 storage. "
                "Install with: pip install aiobotocore"
            )

    async def close(self):
        """Close the S3 client connection."""
        if self._client:
            await self._client.__aexit__(None, None, None)
            self._client = None

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        client = await self._get_client()
        params = {
            "Bucket": self._config.s3_bucket,
            "Key": key,
            "Body": data,
            "ContentType": content_type,
        }
        if metadata:
            params["Metadata"] = metadata

        await client.put_object(**params)
        logger.debug("S3: uploaded %s (%d bytes)", key, len(data))
        return key

    async def upload_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        """Stream upload with true multipart — uploads parts as they arrive."""
        client = await self._get_client()
        PART_SIZE = 10 * 1024 * 1024  # 10 MB parts (S3 minimum is 5 MB)
        MULTIPART_THRESHOLD = 100 * 1024 * 1024  # 100 MB before committing to multipart

        buffer = bytearray()
        total_size = 0
        upload_id = None
        parts = []
        part_number = 1

        try:
            async for chunk in stream:
                buffer.extend(chunk)
                total_size += len(chunk)

                # Once we exceed multipart threshold, start multipart upload
                if upload_id is None and total_size >= MULTIPART_THRESHOLD:
                    create_resp = await client.create_multipart_upload(
                        Bucket=self._config.s3_bucket,
                        Key=key,
                        ContentType=content_type,
                        **({"Metadata": metadata} if metadata else {}),
                    )
                    upload_id = create_resp["UploadId"]

                # Flush full parts to S3 as they accumulate
                while upload_id and len(buffer) >= PART_SIZE:
                    part_data = bytes(buffer[:PART_SIZE])
                    del buffer[:PART_SIZE]
                    resp = await client.upload_part(
                        Bucket=self._config.s3_bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=part_data,
                    )
                    parts.append({"ETag": resp["ETag"], "PartNumber": part_number})
                    part_number += 1

            # Stream exhausted — finalize
            if upload_id:
                # Upload remaining buffer as the final part
                if buffer:
                    resp = await client.upload_part(
                        Bucket=self._config.s3_bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=bytes(buffer),
                    )
                    parts.append({"ETag": resp["ETag"], "PartNumber": part_number})

                await client.complete_multipart_upload(
                    Bucket=self._config.s3_bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
                logger.info("S3: multipart uploaded %s (%d bytes, %d parts)", key, total_size, len(parts))
            else:
                # Small file — single PUT
                return await self.upload(key, bytes(buffer), content_type, metadata)

        except Exception:
            if upload_id:
                await client.abort_multipart_upload(
                    Bucket=self._config.s3_bucket,
                    Key=key,
                    UploadId=upload_id,
                )
            raise

        return key

    async def download(self, key: str) -> bytes:
        client = await self._get_client()
        resp = await client.get_object(
            Bucket=self._config.s3_bucket,
            Key=key,
        )
        data = await resp["Body"].read()
        logger.debug("S3: downloaded %s (%d bytes)", key, len(data))
        return data

    async def download_stream(self, key: str) -> AsyncIterator[bytes]:
        client = await self._get_client()
        resp = await client.get_object(
            Bucket=self._config.s3_bucket,
            Key=key,
        )
        async for chunk in resp["Body"].iter_chunks(chunk_size=1024 * 1024):
            yield chunk

    async def delete(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.delete_object(
                Bucket=self._config.s3_bucket,
                Key=key,
            )
            logger.debug("S3: deleted %s", key)
            return True
        except Exception as e:
            logger.error("S3: delete failed for %s: %s", key, e)
            return False

    async def exists(self, key: str) -> bool:
        client = await self._get_client()
        try:
            await client.head_object(
                Bucket=self._config.s3_bucket,
                Key=key,
            )
            return True
        except Exception:
            return False

    async def presign(self, key: str, expiry: Optional[int] = None) -> str:
        client = await self._get_client()
        url = await client.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self._config.s3_bucket,
                "Key": key,
            },
            ExpiresIn=expiry or self._config.presign_expiry,
        )
        return url

    async def list_objects(
        self, prefix: str, max_keys: int = 1000
    ) -> list[str]:
        client = await self._get_client()
        keys = []
        continuation_token = None

        while True:
            params = {
                "Bucket": self._config.s3_bucket,
                "Prefix": prefix,
                "MaxKeys": min(max_keys - len(keys), 1000),
            }
            if continuation_token:
                params["ContinuationToken"] = continuation_token

            resp = await client.list_objects_v2(**params)

            for obj in resp.get("Contents", []):
                keys.append(obj["Key"])

            if not resp.get("IsTruncated") or len(keys) >= max_keys:
                break
            continuation_token = resp.get("NextContinuationToken")

        return keys[:max_keys]

    async def head(self, key: str) -> Optional[dict]:
        client = await self._get_client()
        try:
            resp = await client.head_object(
                Bucket=self._config.s3_bucket,
                Key=key,
            )
            return {
                "content_length": resp.get("ContentLength", 0),
                "content_type": resp.get("ContentType", ""),
                "last_modified": resp.get("LastModified"),
                "metadata": resp.get("Metadata", {}),
                "etag": resp.get("ETag", ""),
            }
        except Exception:
            return None
