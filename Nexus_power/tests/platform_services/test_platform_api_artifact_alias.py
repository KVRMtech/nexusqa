import pytest
from unittest.mock import MagicMock, patch


class _FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, *args, **kwargs):
        return _FakeResult()


class _FakeClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def get(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_list_session_artifacts_resolves_spine_alias_when_db_empty():
    from app.routers import artifacts as artifacts_router

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "artifact": {
            "artifact_id": "artifact-123",
            "tenant_id": "tenant-1",
            "session_id": "original-session",
            "status": "completed",
            "safe_transcript_text": "secret",
            "full_artifact_json": {"foo": "bar"},
        },
    }
    request = MagicMock()
    request.headers = {"authorization": "Bearer test-token"}

    with patch.object(artifacts_router, "require_db", return_value=_FakeSession):
        with patch("httpx.AsyncClient", return_value=_FakeClient(response)):
            result = await artifacts_router.list_session_artifacts(
                session_id="new-session",
                tenant_id="tenant-1",
                request=request,
            )

    assert result == [{
        "artifact_id": "artifact-123",
        "tenant_id": "tenant-1",
        "session_id": "original-session",
        "status": "completed",
    }]


@pytest.mark.asyncio
async def test_list_session_artifacts_ignores_spine_alias_for_other_tenant():
    from app.routers import artifacts as artifacts_router

    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "success": True,
        "artifact": {
            "artifact_id": "artifact-123",
            "tenant_id": "tenant-2",
            "session_id": "original-session",
            "status": "completed",
        },
    }
    request = MagicMock()
    request.headers = {"authorization": "Bearer test-token"}

    with patch.object(artifacts_router, "require_db", return_value=_FakeSession):
        with patch("httpx.AsyncClient", return_value=_FakeClient(response)):
            result = await artifacts_router.list_session_artifacts(
                session_id="new-session",
                tenant_id="tenant-1",
                request=request,
            )

    assert result == []