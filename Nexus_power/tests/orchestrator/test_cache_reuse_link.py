import importlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SDK_PATH = os.path.join(PROJECT_ROOT, "sdk", "nexus-sdk")
ORCH_PATH = os.path.join(PROJECT_ROOT, "products", "nexus-qa-orchestrator")


@pytest.fixture
def orchestrator_main_module():
    saved_path = sys.path[:]
    saved_modules = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "app" or name.startswith("app.")
    }

    for name in list(sys.modules.keys()):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]

    for path in (SDK_PATH, ORCH_PATH):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)

    try:
        module = importlib.import_module("app.main")
        yield module
    finally:
        for name in list(sys.modules.keys()):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


class _FakeClient:
    def __init__(self, response):
        self._response = response
        self.post_calls = []
        self.patch_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self._response

    async def patch(self, *args, **kwargs):
        self.patch_calls.append((args, kwargs))
        return self._response


@pytest.mark.asyncio
async def test_link_cached_artifact_to_session_posts_reuse_link(orchestrator_main_module):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {"success": True}
    client = _FakeClient(response)

    with patch.object(orchestrator_main_module.httpx, "AsyncClient", return_value=client):
        linked = await orchestrator_main_module._link_cached_artifact_to_session(
            tenant_id="tenant-1",
            session_id="session-1",
            artifact_id="artifact-1",
            spine_url="http://spine:8009",
        )

    assert linked is True
    assert len(client.post_calls) == 1
    args, kwargs = client.post_calls[0]
    assert args[0] == "http://spine:8009/api/v1/spine/artifacts/artifact-1/reuse"
    assert kwargs["json"] == {"tenant_id": "tenant-1", "session_id": "session-1"}
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_link_cached_artifact_to_session_returns_false_on_non_200(orchestrator_main_module):
    response = MagicMock()
    response.status_code = 500
    response.text = "boom"
    response.json.return_value = {"success": False}
    client = _FakeClient(response)

    with patch.object(orchestrator_main_module.httpx, "AsyncClient", return_value=client):
        linked = await orchestrator_main_module._link_cached_artifact_to_session(
            tenant_id="tenant-1",
            session_id="session-1",
            artifact_id="artifact-1",
            spine_url="http://spine:8009",
        )

    assert linked is False


@pytest.mark.asyncio
async def test_update_session_status_patches_platform_api(orchestrator_main_module):
    response = MagicMock()
    response.status_code = 200
    client = _FakeClient(response)

    with patch.dict(os.environ, {"PLATFORM_API_URL": "http://platform-api:8091"}, clear=False):
        with patch.object(orchestrator_main_module.httpx, "AsyncClient", return_value=client):
            updated = await orchestrator_main_module._update_session_status(
                tenant_id="tenant-1",
                session_id="session-1",
                status="completed",
            )

    assert updated is True
    assert len(client.patch_calls) == 1
    args, kwargs = client.patch_calls[0]
    assert args[0] == "http://platform-api:8091/api/v1/sessions/session-1"
    assert kwargs["json"] == {"status": "completed"}
    assert kwargs["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_update_session_status_returns_false_on_non_200(orchestrator_main_module):
    response = MagicMock()
    response.status_code = 500
    response.text = "boom"
    client = _FakeClient(response)

    with patch.dict(os.environ, {"PLATFORM_API_URL": "http://platform-api:8091"}, clear=False):
        with patch.object(orchestrator_main_module.httpx, "AsyncClient", return_value=client):
            updated = await orchestrator_main_module._update_session_status(
                tenant_id="tenant-1",
                session_id="session-1",
                status="cancelled",
            )

    assert updated is False


class _FakeUploadFile:
    def __init__(self, filename: str, content: bytes, content_type: str):
        self.filename = filename
        self.content_type = content_type
        self._content = content

    async def read(self, _size: int = -1):
        return self._content


class _FakeRedis:
    def __init__(self, *, set_result, existing_session=None):
        self._set_result = set_result
        self._existing_session = existing_session

    async def set(self, *args, **kwargs):
        return self._set_result

    async def get(self, *args, **kwargs):
        return self._existing_session


# A REAL minimal WAV header (RIFF ---- WAVE + fmt chunk start).
#
# The upload path validates media by MAGIC BYTES — main.py::_validate_media_content
# requires >=12 bytes and then `RIFF` at 0 with `WAVE` at 8. The previous fixture,
# an 11-byte b"audio-bytes", was rejected with 415 before either test could reach
# the fingerprint-dedup branch it exists to cover, so both were asserting 409 on a
# request that never got that far. Using a genuinely valid header fixes the fixture
# and leaves the content guard fully enforced.
_WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00"


@pytest.mark.asyncio
async def test_start_canonical_processing_marks_cache_hit_session_completed(orchestrator_main_module):
    audio = _FakeUploadFile("dedup.wav", _WAV_BYTES, "audio/wav")
    user = SimpleNamespace(tenant_id="tenant-1", user_id="user-1")

    with patch.object(orchestrator_main_module.file_store, "store", AsyncMock(return_value={"file_id": "audio-1"})):
        with patch.object(orchestrator_main_module.file_store, "read", AsyncMock(return_value=_WAV_BYTES)):
            with patch.object(orchestrator_main_module, "_check_fingerprint_cache", AsyncMock(return_value={
                "artifact_id": "artifact-1",
                "artifact_status": "completed",
                "cache_reason": "fingerprint_match",
            })):
                with patch.object(orchestrator_main_module, "_link_cached_artifact_to_session", AsyncMock(return_value=True)) as link_mock:
                    with patch.object(orchestrator_main_module, "_update_session_status", AsyncMock(return_value=True)) as status_mock:
                        with patch.object(orchestrator_main_module.registry, "get", AsyncMock(return_value=SimpleNamespace(name="Canonical Media Processing"))):
                            result = await orchestrator_main_module.start_canonical_processing(
                                background_tasks=MagicMock(),
                                audio=audio,
                                video=None,
                                session_id="session-1",
                                language="en",
                                num_speakers=None,
                                processing_profile=None,
                                consumer_chain_id=None,
                                user=user,
                            )

    assert result.status == orchestrator_main_module.WorkflowStatus.COMPLETED
    assert result.cache_hit is True
    assert result.cached_artifact_id == "artifact-1"
    link_mock.assert_awaited_once_with(
        tenant_id="tenant-1",
        session_id="session-1",
        artifact_id="artifact-1",
        spine_url=orchestrator_main_module.config.spine_url,
    )
    status_mock.assert_awaited_once_with("tenant-1", "session-1", "completed")


@pytest.mark.asyncio
async def test_start_canonical_processing_marks_duplicate_retry_cancelled(orchestrator_main_module):
    audio = _FakeUploadFile("dedup.wav", _WAV_BYTES, "audio/wav")
    user = SimpleNamespace(tenant_id="tenant-1", user_id="user-1")
    fake_redis = _FakeRedis(set_result=False, existing_session="session-existing")

    with patch.object(orchestrator_main_module.file_store, "store", AsyncMock(return_value={"file_id": "audio-1"})):
        with patch.object(orchestrator_main_module.file_store, "read", AsyncMock(return_value=_WAV_BYTES)):
            with patch.object(orchestrator_main_module, "_check_fingerprint_cache", AsyncMock(return_value=None)):
                with patch.object(orchestrator_main_module, "_update_session_status", AsyncMock(return_value=True)) as status_mock:
                    with patch.object(orchestrator_main_module.workflow_store, "_redis", fake_redis):
                        with patch.object(
                            orchestrator_main_module.workflow_store,
                            "list_instances",
                            AsyncMock(return_value=[SimpleNamespace(status=orchestrator_main_module.WorkflowStatus.RUNNING, workflow_id="wf-existing")]),
                        ):
                            with pytest.raises(HTTPException) as exc_info:
                                await orchestrator_main_module.start_canonical_processing(
                                    background_tasks=MagicMock(),
                                    audio=audio,
                                    video=None,
                                    session_id="session-2",
                                    language="en",
                                    num_speakers=None,
                                    processing_profile=None,
                                    consumer_chain_id=None,
                                    user=user,
                                )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["existing_session_id"] == "session-existing"
    status_mock.assert_awaited_once_with("tenant-1", "session-2", "cancelled")