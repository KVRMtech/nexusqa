"""
Nexus SDK — Azure Blob Storage Backend.

Uses azure-storage-blob.aio for native async I/O.

Required environment:
    AZURE_STORAGE_ACCOUNT      — storage account name
    AZURE_STORAGE_CONTAINER    — container name (default: nexus-artifacts)
And ONE of:
    AZURE_STORAGE_KEY          — shared key auth
    AZURE_STORAGE_CONNECTION_STRING
    AZURE_USE_DEFAULT_CREDENTIAL=true (managed identity / workload identity)
"""

from __future__ import annotations

import logging
import os
from typing import AsyncIterator, Optional
from urllib.parse import quote

from nexus_sdk.storage.base import StorageBackend, StorageConfig

logger = logging.getLogger(__name__)


class AzureBlobStorage(StorageBackend):
    """Azure Blob backend using azure-storage-blob.aio."""

    def __init__(self, config: StorageConfig):
        self._config = config
        self._account = os.environ.get("AZURE_STORAGE_ACCOUNT", "")
        self._container_name = os.environ.get(
            "AZURE_STORAGE_CONTAINER", config.s3_bucket or "nexus-artifacts"
        )
        self._key = os.environ.get("AZURE_STORAGE_KEY", "")
        self._conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
        self._use_default_cred = (
            os.environ.get("AZURE_USE_DEFAULT_CREDENTIAL", "false").lower()
            == "true"
        )
        self._service = None
        self._container = None

    async def _get_container(self):
        if self._container is not None:
            return self._container

        from azure.storage.blob.aio import BlobServiceClient

        if self._conn_str:
            self._service = BlobServiceClient.from_connection_string(
                self._conn_str
            )
        elif self._use_default_cred:
            from azure.identity.aio import DefaultAzureCredential

            self._service = BlobServiceClient(
                account_url=f"https://{self._account}.blob.core.windows.net",
                credential=DefaultAzureCredential(),
            )
        else:
            self._service = BlobServiceClient(
                account_url=f"https://{self._account}.blob.core.windows.net",
                credential=self._key,
            )

        self._container = self._service.get_container_client(
            self._container_name
        )

        try:
            await self._container.create_container()
            logger.info(
                "Azure: created container %s", self._container_name
            )
        except Exception as e:
            if "ContainerAlreadyExists" not in str(e):
                logger.debug("Azure: container check: %s", e)
        return self._container

    async def close(self):
        if self._service:
            await self._service.close()
            self._service = None
            self._container = None

    async def upload(
        self,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        from azure.storage.blob import ContentSettings

        container = await self._get_container()
        await container.upload_blob(
            name=key,
            data=data,
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
            metadata=metadata or None,
        )
        logger.debug("Azure: uploaded %s (%d bytes)", key, len(data))
        return key

    async def upload_stream(
        self,
        key: str,
        stream: AsyncIterator[bytes],
        content_type: str = "application/octet-stream",
        metadata: Optional[dict[str, str]] = None,
    ) -> str:
        from azure.storage.blob import ContentSettings

        container = await self._get_container()
        blob_client = container.get_blob_client(key)

        async def _gen():
            async for chunk in stream:
                yield chunk

        await blob_client.upload_blob(
            data=_gen(),
            overwrite=True,
            content_settings=ContentSettings(content_type=content_type),
            metadata=metadata or None,
            max_concurrency=4,
        )
        return key

    async def download(self, key: str) -> bytes:
        container = await self._get_container()
        blob_client = container.get_blob_client(key)
        downloader = await blob_client.download_blob()
        return await downloader.readall()

    async def download_stream(self, key: str) -> AsyncIterator[bytes]:
        container = await self._get_container()
        blob_client = container.get_blob_client(key)
        downloader = await blob_client.download_blob()
        async for chunk in downloader.chunks():
            yield chunk

    async def delete(self, key: str) -> bool:
        container = await self._get_container()
        try:
            await container.delete_blob(key)
            return True
        except Exception as e:
            logger.error("Azure: delete failed for %s: %s", key, e)
            return False

    async def exists(self, key: str) -> bool:
        container = await self._get_container()
        try:
            blob_client = container.get_blob_client(key)
            return await blob_client.exists()
        except Exception:
            return False

    async def presign(self, key: str, expiry: Optional[int] = None) -> str:
        from datetime import datetime, timedelta, timezone
        from azure.storage.blob import (
            BlobSasPermissions,
            generate_blob_sas,
        )

        ttl = expiry or self._config.presign_expiry
        expiry_dt = datetime.now(timezone.utc) + timedelta(seconds=ttl)

        if self._conn_str or self._key:
            sas = generate_blob_sas(
                account_name=self._account,
                container_name=self._container_name,
                blob_name=key,
                account_key=self._key,
                permission=BlobSasPermissions(read=True),
                expiry=expiry_dt,
            )
            return (
                f"https://{self._account}.blob.core.windows.net/"
                f"{self._container_name}/{quote(key)}?{sas}"
            )

        # Default-credential / managed-identity path: use a user-delegation key
        from azure.storage.blob import generate_blob_sas as _gen
        service = (await self._get_container()).service_client
        udk = await service.get_user_delegation_key(
            key_start_time=datetime.now(timezone.utc),
            key_expiry_time=expiry_dt,
        )
        sas = _gen(
            account_name=self._account,
            container_name=self._container_name,
            blob_name=key,
            user_delegation_key=udk,
            permission=BlobSasPermissions(read=True),
            expiry=expiry_dt,
        )
        return (
            f"https://{self._account}.blob.core.windows.net/"
            f"{self._container_name}/{quote(key)}?{sas}"
        )

    async def list_objects(
        self, prefix: str, max_keys: int = 1000
    ) -> list[str]:
        container = await self._get_container()
        keys: list[str] = []
        async for blob in container.list_blobs(name_starts_with=prefix):
            keys.append(blob.name)
            if len(keys) >= max_keys:
                break
        return keys

    async def head(self, key: str) -> Optional[dict]:
        container = await self._get_container()
        try:
            blob_client = container.get_blob_client(key)
            props = await blob_client.get_blob_properties()
            return {
                "content_length": props.size,
                "content_type": (
                    props.content_settings.content_type
                    if props.content_settings else ""
                ),
                "last_modified": props.last_modified,
                "metadata": props.metadata or {},
                "etag": props.etag or "",
            }
        except Exception:
            return None
