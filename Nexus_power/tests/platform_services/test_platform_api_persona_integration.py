"""
Integration tests — Persona routes with live DB + mocked Brain.

These tests spin up an in-memory SQLite database, create real tables,
seed real data, and exercise the full HTTP request → DB → response cycle.

Brain engine calls are mocked so we test everything EXCEPT the LLM.

Coverage:
  - Cache write + read roundtrip
  - force_regenerate bypasses cache
  - Fallback draft is cached with draft_quality="fallback"
  - Tenant isolation on generate-draft, GET, PUT, DELETE
  - System persona readable by any tenant
  - System persona not modifiable / not deletable

NOTE: All ``app.*`` imports are deferred to fixture/test time because the
module-scoped ``_isolate_app_module`` conftest fixture must patch sys.path
before ``app`` can be resolved.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus_sdk.db import Base
from nexus_sdk.db.models import (
    CanonicalArtifactRow,
    PersonaRow,
    TenantRow,
)

# ── Constants ──────────────────────────────────────────────────

# SIGN WITH THE SECRET THE APP WILL VERIFY WITH, whatever it is.
#
# This was the literal "dev-jwt-secret-change-me". The application reads its
# signing key from NEXUS_JWT_SECRET and only falls back to that literal when the
# variable is unset — true on a developer laptop, false in CI, which exports
# NEXUS_JWT_SECRET for every job. So every token this file minted was signed with
# the wrong key and the API correctly rejected all of them: twenty tests failing
# as `assert 401 == 200`, reported as broken tenant-isolation and broken Brain
# error handling when the only thing broken was the fixture's key.
#
# Reading the same variable keeps the suite honest in both places, and does NOT
# weaken the auth assertions: the 401 paths are proven separately by
# test_invalid_token_returns_401, which sends deliberate garbage.
JWT_SECRET = os.getenv("NEXUS_JWT_SECRET", "dev-jwt-secret-change-me")
JWT_ALG = "HS256"

TENANT_A = "tenant-alpha"
TENANT_B = "tenant-beta"
SYSTEM_TENANT = "__system__"

ARTIFACT_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid4())


# ── Helpers ────────────────────────────────────────────────────

def _make_token(tenant_id: str, user_id: str = "u-1", role: str = "admin") -> str:
    now = int(time.time())
    payload = {
        "sub": user_id,
        "tenant_id": tenant_id,
        "email": f"{user_id}@test.local",
        "role": role,
        "iat": now,
        "exp": now + 3600,
    }
    return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def _auth(tenant_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_make_token(tenant_id)}"}


def _fake_brain_response(*, evidence: bool = True) -> dict:
    """Return a mock Brain response with or without evidence."""
    domain_map = (
        {
            "actors": [{"name": "Pharmacist", "source": "transcript"}],
            "systems": [{"name": "POS", "source": "visual"}],
            "entities": [],
            "workflows": [{"name": "Dispense", "source": "transcript"}],
            "decisions": [],
            "risks": [{"name": "Wrong dosage", "source": "transcript"}],
            "unknowns": [],
        }
        if evidence
        else {
            "actors": [],
            "systems": [],
            "entities": [],
            "workflows": [],
            "decisions": [],
            "risks": [],
            "unknowns": [],
        }
    )
    return {
        "persona": {
            "name": "Test Expert",
            "description": "A test persona",
            "system_prompt": "You are a test expert.",
            "capabilities": ["rule_extraction"],
            "specialty_domains": ["pharmacy"],
            "avatar_icon": "brain",
            "stage_config": {},
        },
        "domain_map": domain_map,
        "grounding_contract": {
            "total_evidence_count": 4 if evidence else 0,
            "modality_distribution": {"transcript": 3, "visual": 1} if evidence else {},
            "avg_confidence": 0.85 if evidence else 0.0,
            "open_questions": [],
        },
        "provenance": {
            "artifact_id": ARTIFACT_ID,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "model_used": "test-mock",
            "model_backend": "mock",
            "generation_time_ms": 42,
        },
    }


# ── Fixtures ───────────────────────────────────────────────────

@pytest_asyncio.fixture()
async def db_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Create an in-memory SQLite database with all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture()
async def seeded_db(db_factory):
    """Seed tenants + a completed artifact for TENANT_A."""
    async with db_factory() as session:
        session.add(TenantRow(tenant_id=TENANT_A, name="Alpha Corp", domain="alpha.test"))
        session.add(TenantRow(tenant_id=TENANT_B, name="Beta Corp", domain="beta.test"))
        session.add(
            TenantRow(tenant_id=SYSTEM_TENANT, name="System", domain="system.internal")
        )
        session.add(
            CanonicalArtifactRow(
                artifact_id=ARTIFACT_ID,
                tenant_id=TENANT_A,
                session_id=SESSION_ID,
                status="completed",
                safe_transcript_text="Pharmacist dispenses medication to patient.",
                visual_summary="POS screen showing dispense workflow.",
                application_types_seen=["pharmacy"],
                full_artifact_json={},
                duration_seconds=120.0,
                scene_count=3,
                frame_count=15,
            )
        )
        await session.commit()
    return db_factory


@pytest_asyncio.fixture()
async def app_client(seeded_db):
    """
    Build a FastAPI test app wired to the in-memory DB, with Brain mocked.
    Returns (AsyncClient, mock_brain_post) so tests can configure Brain responses.
    """
    from fastapi import FastAPI
    from app.config import PlatformAPIConfig
    from app.routers import personas as personas_module

    app = FastAPI()
    app.state.config = PlatformAPIConfig()  # picks up defaults
    app.include_router(personas_module.router)

    # Patch require_db to return our in-memory factory
    with patch.object(personas_module, "require_db", return_value=seeded_db):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, seeded_db


# ── Shortcut fixture that also patches Brain httpx ─────────────

class _FakeBrainResponse:
    """Mimics httpx.Response enough for the route code."""

    def __init__(self, data: dict, status_code: int = 200):
        self._data = data
        self.status_code = status_code
        self.text = json.dumps(data)

    def json(self):
        return self._data


@pytest_asyncio.fixture()
async def full_client(seeded_db):
    """
    App client with both DB and Brain httpx mocked.
    Returns (AsyncClient, brain_response_setter).
    """
    from fastapi import FastAPI
    from app.config import PlatformAPIConfig
    from app.routers import personas as personas_module

    app = FastAPI()
    app.state.config = PlatformAPIConfig()
    app.include_router(personas_module.router)

    # Default Brain response
    brain_data = _fake_brain_response(evidence=True)
    fake_resp = _FakeBrainResponse(brain_data)

    # Build an async context manager mock for httpx.AsyncClient
    mock_post = AsyncMock(return_value=fake_resp)
    mock_httpx_instance = AsyncMock()
    mock_httpx_instance.post = mock_post
    mock_httpx_instance.__aenter__ = AsyncMock(return_value=mock_httpx_instance)
    mock_httpx_instance.__aexit__ = AsyncMock(return_value=False)

    mock_httpx_cls = MagicMock(return_value=mock_httpx_instance)

    def set_brain_response(data: dict, status_code: int = 200):
        new_resp = _FakeBrainResponse(data, status_code)
        mock_post.return_value = new_resp

    with (
        patch.object(personas_module, "require_db", return_value=seeded_db),
        patch.object(personas_module, "httpx") as patched_httpx,
    ):
        patched_httpx.AsyncClient = mock_httpx_cls
        patched_httpx.TimeoutException = __import__("httpx").TimeoutException
        patched_httpx.ConnectError = __import__("httpx").ConnectError
        patched_httpx.RemoteProtocolError = __import__("httpx").RemoteProtocolError

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, set_brain_response, mock_post


# ═══════════════════════════════════════════════════════════════
#  CACHE PERSISTENCE TESTS
# ═══════════════════════════════════════════════════════════════


class TestCacheRoundtrip:
    """Verify generate → cache write → cache read cycle."""

    @pytest.mark.asyncio
    async def test_first_call_hits_brain_and_caches(self, full_client):
        client, _, mock_post = full_client

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cached"] is False
        assert data["persona"]["name"] == "Test Expert"
        assert data["draft_quality"] == "full"
        mock_post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_second_call_returns_cache(self, full_client):
        client, _, mock_post = full_client

        # First call → writes cache
        r1 = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert r1.status_code == 200
        assert r1.json()["cached"] is False

        # Second call → should hit cache, NOT call Brain again
        mock_post.reset_mock()
        r2 = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert r2.status_code == 200
        data = r2.json()
        assert data["cached"] is True
        assert "cache_hit_ms" in data
        assert data["persona"]["name"] == "Test Expert"
        mock_post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_regenerate_bypasses_cache(self, full_client):
        client, _, mock_post = full_client

        # First call → writes cache
        r1 = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert r1.status_code == 200

        # Force regenerate → should call Brain again
        mock_post.reset_mock()
        r2 = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID, "force_regenerate": True},
            headers=_auth(TENANT_A),
        )
        assert r2.status_code == 200
        assert r2.json()["cached"] is False
        mock_post.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_fallback_draft_is_cached_with_quality_tag(self, full_client):
        client, set_brain, mock_post = full_client

        # Brain returns empty evidence → fallback quality
        set_brain(_fake_brain_response(evidence=False))

        r1 = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert r1.status_code == 200
        assert r1.json()["draft_quality"] == "fallback"

        # Second call → cache hit with fallback quality preserved
        mock_post.reset_mock()
        r2 = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert r2.status_code == 200
        assert r2.json()["cached"] is True
        assert r2.json()["draft_quality"] == "fallback"
        mock_post.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════
#  TENANT ISOLATION — GENERATE DRAFT
# ═══════════════════════════════════════════════════════════════


class TestGenerateDraftTenantIsolation:
    """Artifact belongs to TENANT_A — TENANT_B must get 404."""

    @pytest.mark.asyncio
    async def test_wrong_tenant_cannot_generate_draft(self, full_client):
        client, _, _ = full_client

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_B),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_correct_tenant_can_generate_draft(self, full_client):
        client, _, _ = full_client

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════════
#  TENANT ISOLATION — PERSONA CRUD
# ═══════════════════════════════════════════════════════════════


class TestPersonaCRUDTenantIsolation:
    """Create a persona in TENANT_A, then verify TENANT_B is blocked."""

    @pytest.mark.asyncio
    async def test_get_persona_wrong_tenant_404(self, app_client):
        client, db_factory = app_client

        # Create persona via TENANT_A
        resp = await client.post(
            "/api/v1/personas",
            json={"name": "My Persona", "slug": "my-persona"},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 201
        pid = resp.json()["persona_id"]

        # TENANT_B cannot GET it
        resp2 = await client.get(
            f"/api/v1/personas/{pid}",
            headers=_auth(TENANT_B),
        )
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_update_persona_wrong_tenant_404(self, app_client):
        client, _ = app_client

        resp = await client.post(
            "/api/v1/personas",
            json={"name": "Tenant A Only", "slug": "ta-only"},
            headers=_auth(TENANT_A),
        )
        pid = resp.json()["persona_id"]

        # TENANT_B cannot UPDATE it
        resp2 = await client.put(
            f"/api/v1/personas/{pid}",
            json={"name": "Hacked"},
            headers=_auth(TENANT_B),
        )
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_persona_wrong_tenant_404(self, app_client):
        client, _ = app_client

        resp = await client.post(
            "/api/v1/personas",
            json={"name": "Delete Me", "slug": "delete-me"},
            headers=_auth(TENANT_A),
        )
        pid = resp.json()["persona_id"]

        # TENANT_B cannot DELETE it
        resp2 = await client.delete(
            f"/api/v1/personas/{pid}",
            headers=_auth(TENANT_B),
        )
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_list_personas_only_shows_own_and_system(self, app_client):
        client, db_factory = app_client

        # Seed a system persona directly
        async with db_factory() as sess:
            sess.add(
                PersonaRow(
                    persona_id=str(uuid.uuid4()),
                    tenant_id=SYSTEM_TENANT,
                    name="System QA",
                    slug="system-qa",
                    is_system=True,
                    is_active=True,
                    sort_order=10,
                )
            )
            await sess.commit()

        # Create a TENANT_A persona
        await client.post(
            "/api/v1/personas",
            json={"name": "Alpha Custom", "slug": "alpha-custom"},
            headers=_auth(TENANT_A),
        )

        # TENANT_B list should only show system, not TENANT_A customs
        resp = await client.get(
            "/api/v1/personas",
            headers=_auth(TENANT_B),
        )
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert "System QA" in names
        assert "Alpha Custom" not in names


# ═══════════════════════════════════════════════════════════════
#  SYSTEM PERSONA PROTECTION
# ═══════════════════════════════════════════════════════════════


class TestSystemPersonaProtection:
    """System personas are read-only: visible to all, modifiable by none."""

    @pytest_asyncio.fixture()
    async def system_persona(self, app_client):
        client, db_factory = app_client
        pid = str(uuid.uuid4())
        async with db_factory() as sess:
            sess.add(
                PersonaRow(
                    persona_id=pid,
                    tenant_id=SYSTEM_TENANT,
                    name="Built-In Expert",
                    slug="builtin-expert",
                    is_system=True,
                    is_active=True,
                    sort_order=1,
                )
            )
            await sess.commit()
        return pid, client

    @pytest.mark.asyncio
    async def test_any_tenant_can_read_system_persona(self, system_persona):
        pid, client = system_persona

        for tenant in [TENANT_A, TENANT_B]:
            resp = await client.get(
                f"/api/v1/personas/{pid}",
                headers=_auth(tenant),
            )
            assert resp.status_code == 200
            assert resp.json()["name"] == "Built-In Expert"

    @pytest.mark.asyncio
    async def test_system_persona_cannot_be_updated(self, system_persona):
        pid, client = system_persona

        resp = await client.put(
            f"/api/v1/personas/{pid}",
            json={"name": "Hacked System"},
            headers=_auth(TENANT_A),
        )
        # System persona's tenant_id is __system__, not TENANT_A → 404 from tenant check
        # (The route checks tenant_id match first, then is_system)
        assert resp.status_code in (403, 404)

    @pytest.mark.asyncio
    async def test_system_persona_cannot_be_deleted(self, system_persona):
        pid, client = system_persona

        resp = await client.delete(
            f"/api/v1/personas/{pid}",
            headers=_auth(TENANT_A),
        )
        assert resp.status_code in (403, 404)


# ═══════════════════════════════════════════════════════════════
#  EDGE CASES
# ═══════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Misc edge cases for coverage."""

    @pytest.mark.asyncio
    async def test_no_auth_header_returns_401(self, app_client):
        client, _ = app_client
        resp = await client.get("/api/v1/personas")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_invalid_token_returns_401(self, app_client):
        client, _ = app_client
        resp = await client.get(
            "/api/v1/personas",
            headers={"Authorization": "Bearer garbage.token.here"},
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_generate_draft_nonexistent_artifact_404(self, full_client):
        client, _, _ = full_client
        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": str(uuid.uuid4())},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_brain_timeout_returns_504(self, seeded_db):
        """When Brain times out, platform should return 504."""
        import httpx as real_httpx
        from fastapi import FastAPI
        from app.config import PlatformAPIConfig
        from app.routers import personas as personas_module

        app = FastAPI()
        app.state.config = PlatformAPIConfig()
        app.include_router(personas_module.router)

        # Make httpx.AsyncClient raise TimeoutException
        mock_httpx_instance = AsyncMock()
        mock_httpx_instance.post = AsyncMock(
            side_effect=real_httpx.TimeoutException("timed out")
        )
        mock_httpx_instance.__aenter__ = AsyncMock(return_value=mock_httpx_instance)
        mock_httpx_instance.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls = MagicMock(return_value=mock_httpx_instance)

        with (
            patch.object(personas_module, "require_db", return_value=seeded_db),
            patch.object(personas_module, "httpx") as patched_httpx,
        ):
            patched_httpx.AsyncClient = mock_httpx_cls
            patched_httpx.TimeoutException = real_httpx.TimeoutException
            patched_httpx.ConnectError = real_httpx.ConnectError

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/personas/generate-draft",
                    json={"artifact_id": ARTIFACT_ID},
                    headers=_auth(TENANT_A),
                )
                assert resp.status_code == 504

    @pytest.mark.asyncio
    async def test_brain_connect_error_returns_503(self, seeded_db):
        """When Brain is unreachable, platform should return 503."""
        import httpx as real_httpx
        from fastapi import FastAPI
        from app.config import PlatformAPIConfig
        from app.routers import personas as personas_module

        app = FastAPI()
        app.state.config = PlatformAPIConfig()
        app.include_router(personas_module.router)

        mock_httpx_instance = AsyncMock()
        mock_httpx_instance.post = AsyncMock(
            side_effect=real_httpx.ConnectError("unreachable")
        )
        mock_httpx_instance.__aenter__ = AsyncMock(return_value=mock_httpx_instance)
        mock_httpx_instance.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls = MagicMock(return_value=mock_httpx_instance)

        with (
            patch.object(personas_module, "require_db", return_value=seeded_db),
            patch.object(personas_module, "httpx") as patched_httpx,
        ):
            patched_httpx.AsyncClient = mock_httpx_cls
            patched_httpx.TimeoutException = real_httpx.TimeoutException
            patched_httpx.ConnectError = real_httpx.ConnectError

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/personas/generate-draft",
                    json={"artifact_id": ARTIFACT_ID},
                    headers=_auth(TENANT_A),
                )
                assert resp.status_code == 503


# ═══════════════════════════════════════════════════════════════
#  RUNTIME FAILURE SCENARIOS
# ═══════════════════════════════════════════════════════════════


class TestRuntimeFailures:
    """Tests for runtime failure modes the architect identified."""

    @pytest.mark.asyncio
    async def test_brain_remote_protocol_error_returns_502(self, seeded_db):
        """When Brain drops the connection mid-response, platform returns 502."""
        import httpx as real_httpx
        from fastapi import FastAPI
        from app.config import PlatformAPIConfig
        from app.routers import personas as personas_module

        app = FastAPI()
        app.state.config = PlatformAPIConfig()
        app.include_router(personas_module.router)

        mock_httpx_instance = AsyncMock()
        mock_httpx_instance.post = AsyncMock(
            side_effect=real_httpx.RemoteProtocolError("peer closed connection")
        )
        mock_httpx_instance.__aenter__ = AsyncMock(return_value=mock_httpx_instance)
        mock_httpx_instance.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls = MagicMock(return_value=mock_httpx_instance)

        with (
            patch.object(personas_module, "require_db", return_value=seeded_db),
            patch.object(personas_module, "httpx") as patched_httpx,
        ):
            patched_httpx.AsyncClient = mock_httpx_cls
            patched_httpx.TimeoutException = real_httpx.TimeoutException
            patched_httpx.ConnectError = real_httpx.ConnectError
            patched_httpx.RemoteProtocolError = real_httpx.RemoteProtocolError

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/personas/generate-draft",
                    json={"artifact_id": ARTIFACT_ID},
                    headers=_auth(TENANT_A),
                )
                assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_brain_http_error_returns_502(self, seeded_db):
        """When Brain returns a non-200 HTTP status, platform returns 502."""
        from fastapi import FastAPI
        from app.config import PlatformAPIConfig
        from app.routers import personas as personas_module

        app = FastAPI()
        app.state.config = PlatformAPIConfig()
        app.include_router(personas_module.router)

        fake_resp = _FakeBrainResponse({"error": "internal"}, status_code=500)
        mock_httpx_instance = AsyncMock()
        mock_httpx_instance.post = AsyncMock(return_value=fake_resp)
        mock_httpx_instance.__aenter__ = AsyncMock(return_value=mock_httpx_instance)
        mock_httpx_instance.__aexit__ = AsyncMock(return_value=False)
        mock_httpx_cls = MagicMock(return_value=mock_httpx_instance)

        with (
            patch.object(personas_module, "require_db", return_value=seeded_db),
            patch.object(personas_module, "httpx") as patched_httpx,
        ):
            patched_httpx.AsyncClient = mock_httpx_cls
            patched_httpx.TimeoutException = __import__("httpx").TimeoutException
            patched_httpx.ConnectError = __import__("httpx").ConnectError
            patched_httpx.RemoteProtocolError = __import__("httpx").RemoteProtocolError

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as c:
                resp = await c.post(
                    "/api/v1/personas/generate-draft",
                    json={"artifact_id": ARTIFACT_ID},
                    headers=_auth(TENANT_A),
                )
                assert resp.status_code == 502

    @pytest.mark.asyncio
    async def test_stub_backend_tagged_as_fallback(self, full_client):
        """When Brain falls back to stub, quality must be 'fallback'."""
        client, set_brain, _ = full_client

        stub_response = _fake_brain_response(evidence=False)
        stub_response["provenance"]["model_backend"] = "stub"
        stub_response["provenance"]["model_used"] = "stub-fallback"
        set_brain(stub_response)

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["draft_quality"] == "fallback"
        assert data["provenance"]["model_backend"] == "stub"

    @pytest.mark.asyncio
    async def test_stub_with_evidence_still_tagged_fallback(self, full_client):
        """Even if stub somehow has evidence, model_backend=stub forces fallback."""
        client, set_brain, _ = full_client

        # Stub response but inject fake evidence
        stub_response = _fake_brain_response(evidence=True)
        stub_response["provenance"]["model_backend"] = "stub"
        set_brain(stub_response)

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200
        assert resp.json()["draft_quality"] == "fallback"

    @pytest.mark.asyncio
    async def test_real_model_with_evidence_tagged_full(self, full_client):
        """Non-stub response with evidence should be tagged 'full'."""
        client, set_brain, _ = full_client

        real_response = _fake_brain_response(evidence=True)
        real_response["provenance"]["model_backend"] = "ollama"
        set_brain(real_response)

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200
        assert resp.json()["draft_quality"] == "full"

    @pytest.mark.asyncio
    async def test_real_model_no_evidence_tagged_fallback(self, full_client):
        """Non-stub response without evidence should be tagged 'fallback'."""
        client, set_brain, _ = full_client

        real_response = _fake_brain_response(evidence=False)
        real_response["provenance"]["model_backend"] = "ollama"
        set_brain(real_response)

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200
        assert resp.json()["draft_quality"] == "fallback"

    @pytest.mark.asyncio
    async def test_generation_time_ms_in_provenance(self, full_client):
        """Provenance should include generation_time_ms from Brain."""
        client, set_brain, _ = full_client

        response = _fake_brain_response(evidence=True)
        response["provenance"]["generation_time_ms"] = 4200.55
        set_brain(response)

        resp = await client.post(
            "/api/v1/personas/generate-draft",
            json={"artifact_id": ARTIFACT_ID},
            headers=_auth(TENANT_A),
        )
        assert resp.status_code == 200
        assert resp.json()["provenance"]["generation_time_ms"] == 4200.55
