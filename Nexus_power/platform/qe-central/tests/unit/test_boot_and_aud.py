"""QE-Central Phase-6 SAFETY SPINE — boot gate + JWT audience + KEK canary.

Pins the fail-closed foundation that must hold BEFORE any real client's app or
credentials touch the system:

  * :func:`app.security.boot_validator.validate_boot_safety` REFUSES to boot a
    DEPLOYED process (``NEXUS_ENV`` in {staging, production}) that is still
    wearing development defaults (dev KEK / default JWT|explorer secrets /
    default DB password), and is a NO-OP-WARN in development/test.
  * The JWT ``aud`` gate (no VKPower<->Verdict token bleed): a minted token
    carries the audience when asked; a foreign ``aud`` is ALWAYS rejected; a
    missing ``aud`` is transition-accepted unless ``QEC_REQUIRE_AUD``.
  * The ``/health`` KEK canary block + 'degraded' logic.

No database or network is touched.  The env default is ``NEXUS_ENV=test``
(conftest), so the whole suite proves the gates are INERT in dev/test.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth import _decode_token, jwt_auth_middleware, require_auth
from app.config import AUDIENCE, settings
from app.security.boot_validator import (
    BootSafetyError,
    collect_boot_violations,
    kek_health_status,
    validate_boot_safety,
)
from app.service_token import mint_service_jwt

SECRET = settings.nexus_jwt_secret


# ─── helpers ───────────────────────────────────────────────────────────


def _safe_prod_settings(**overrides):
    """A settings-like object whose defaults are ALL production-safe.

    Individual tests flip ONE field to a development default to prove that field
    is caught.  ``nexus_env`` defaults to 'production' so the gate bites unless a
    test overrides it.
    """
    base = dict(
        nexus_env="production",
        nexus_kek_provider="gcp_kms",
        nexus_jwt_secret="R7-strong-random-secret-9f3c1a8e42b7d605",
        qec_explorer_token="R7-strong-explorer-token-1b9d4c7e30a6f251",
        qec_database_url="postgresql+asyncpg://qec:Str0ng-Db-Pw-A@db:5432/qecentral",
        nexus_database_url_substrate=(
            "postgresql+asyncpg://qec_substrate:Str0ng-Db-Pw-B@db:5432/nexus"
        ),
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _raw_token(
    *,
    tenant_id: str | None = "tenant-a",
    role: str = "manager",
    aud: str | None = None,
    secret: str | None = None,
    exp_delta: timedelta = timedelta(minutes=10),
) -> str:
    """Encode a JWT directly (bypassing mint) to control the ``aud`` claim."""
    claims: dict = {
        "sub": "user-x",
        "email": "user@example.test",
        "role": role,
        "exp": datetime.now(timezone.utc) + exp_delta,
    }
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    if aud is not None:
        claims["aud"] = aud
    return pyjwt.encode(claims, secret or SECRET, algorithm="HS256")


@pytest.fixture()
def middleware_client() -> TestClient:
    """Minimal app wearing the REAL jwt middleware + require_auth dependency."""
    app = FastAPI()
    app.middleware("http")(jwt_auth_middleware)

    @app.get("/api/v1/qec/whoami")
    async def whoami(user: dict = Depends(require_auth)):
        return user

    return TestClient(app)


# ═══════════════════════════════════════════════════════════════════════
# 1. Boot validator — pure violation collection
# ═══════════════════════════════════════════════════════════════════════


class TestCollectBootViolations:
    def test_all_safe_is_empty(self):
        assert collect_boot_violations(_safe_prod_settings()) == []

    def test_local_kek_flagged(self):
        v = collect_boot_violations(_safe_prod_settings(nexus_kek_provider="local"))
        assert any("NEXUS_KEK_PROVIDER" in m for m in v)
        assert len(v) == 1

    @pytest.mark.parametrize(
        "secret",
        ["", "dev-jwt-secret-change-me", "test-secret-do-not-use-in-production",
         "unit-test-secret-qe-central", "changeme"],
    )
    def test_default_jwt_secret_flagged(self, secret):
        v = collect_boot_violations(_safe_prod_settings(nexus_jwt_secret=secret))
        assert any("NEXUS_JWT_SECRET" in m for m in v)

    @pytest.mark.parametrize("token", ["", "dev-explorer-token-change-me"])
    def test_default_explorer_token_flagged(self, token):
        v = collect_boot_violations(_safe_prod_settings(qec_explorer_token=token))
        assert any("QEC_EXPLORER_TOKEN" in m for m in v)

    @pytest.mark.parametrize(
        "dsn,label",
        [
            ("postgresql+asyncpg://qec:qec-dev@postgres:5432/qecentral", "QEC_DATABASE_URL"),
            ("postgresql+asyncpg://qec:postgres@postgres:5432/qecentral", "QEC_DATABASE_URL"),
        ],
    )
    def test_default_db_password_flagged(self, dsn, label):
        v = collect_boot_violations(_safe_prod_settings(qec_database_url=dsn))
        assert any(label in m for m in v)

    def test_substrate_default_db_password_flagged(self):
        v = collect_boot_violations(
            _safe_prod_settings(
                nexus_database_url_substrate=(
                    "postgresql+asyncpg://qec_substrate:qec-substrate-dev@p:5432/nexus"
                )
            )
        )
        assert any("NEXUS_DATABASE_URL_SUBSTRATE" in m for m in v)

    def test_strong_db_password_not_flagged(self):
        # A real password that merely CONTAINS a marker substring must not trip
        # the check — only an exact dev-default password does.
        v = collect_boot_violations(
            _safe_prod_settings(
                qec_database_url="postgresql+asyncpg://qec:qec-dev-but-actually-strong-x9@d:5432/q"
            )
        )
        assert v == []

    def test_is_env_agnostic(self):
        # collect_* reports what is unsafe regardless of env (development here).
        unsafe = _safe_prod_settings(nexus_env="development", nexus_kek_provider="local")
        assert any("NEXUS_KEK_PROVIDER" in m for m in collect_boot_violations(unsafe))

    def test_never_leaks_secret_values(self):
        s = _safe_prod_settings(
            nexus_kek_provider="local",
            nexus_jwt_secret="dev-jwt-secret-change-me",
            qec_explorer_token="dev-explorer-token-change-me",
            qec_database_url="postgresql+asyncpg://qec:qec-dev@postgres:5432/qecentral",
        )
        joined = " || ".join(collect_boot_violations(s))
        # The setting NAMES appear; the secret/password VALUES never do.
        assert "dev-jwt-secret-change-me" not in joined
        assert "dev-explorer-token-change-me" not in joined
        assert "qec-dev" not in joined


# ═══════════════════════════════════════════════════════════════════════
# 2. validate_boot_safety — fail-closed in deployed envs, warn-only in dev/test
# ═══════════════════════════════════════════════════════════════════════


class TestValidateBootSafety:
    def test_production_local_kek_raises(self):
        with pytest.raises(BootSafetyError) as ei:
            validate_boot_safety(_safe_prod_settings(nexus_kek_provider="local"))
        assert ei.value.violations
        assert any("NEXUS_KEK_PROVIDER" in m for m in ei.value.violations)

    def test_production_default_secret_raises(self):
        with pytest.raises(BootSafetyError):
            validate_boot_safety(
                _safe_prod_settings(nexus_jwt_secret="dev-jwt-secret-change-me")
            )

    def test_production_default_explorer_token_raises(self):
        with pytest.raises(BootSafetyError):
            validate_boot_safety(_safe_prod_settings(qec_explorer_token=""))

    def test_production_default_db_password_raises(self):
        with pytest.raises(BootSafetyError):
            validate_boot_safety(
                _safe_prod_settings(
                    qec_database_url="postgresql+asyncpg://qec:qec-dev@p:5432/qecentral"
                )
            )

    def test_staging_also_bites(self):
        with pytest.raises(BootSafetyError):
            validate_boot_safety(
                _safe_prod_settings(nexus_env="staging", nexus_kek_provider="local")
            )

    def test_production_all_safe_no_raise(self):
        assert validate_boot_safety(_safe_prod_settings()) == []

    def test_development_warns_only(self):
        # Every dev default present, but development ⇒ NEVER raises (returns list).
        result = validate_boot_safety(
            _safe_prod_settings(
                nexus_env="development",
                nexus_kek_provider="local",
                nexus_jwt_secret="dev-jwt-secret-change-me",
                qec_explorer_token="",
                qec_database_url="postgresql+asyncpg://qec:qec-dev@p:5432/qecentral",
            )
        )
        assert result  # non-empty (the warnings), but no exception raised

    def test_test_env_warns_only(self):
        result = validate_boot_safety(
            _safe_prod_settings(nexus_env="test", nexus_kek_provider="local")
        )
        assert result and isinstance(result, list)

    def test_unknown_env_treated_as_non_deployed(self):
        # An unrecognised env is NOT deployed ⇒ warn-only (fail-safe-for-dev).
        result = validate_boot_safety(
            _safe_prod_settings(nexus_env="qa-sandbox", nexus_kek_provider="local")
        )
        assert result and isinstance(result, list)

    def test_error_message_lists_count(self):
        try:
            validate_boot_safety(
                _safe_prod_settings(nexus_kek_provider="local", qec_explorer_token="")
            )
        except BootSafetyError as exc:
            assert "refuses to boot" in str(exc)
            assert len(exc.violations) == 2


# ═══════════════════════════════════════════════════════════════════════
# 3. JWT audience — mint side
# ═══════════════════════════════════════════════════════════════════════


class TestMintAudience:
    def test_default_mint_has_no_aud(self):
        # CRITICAL for backward-compat: the token relayed to the UNCHANGED
        # VKPower factory must carry NO aud (VKPower's decode has no audience arg
        # and would 401 an aud-bearing token). Default mint = no aud.
        token = mint_service_jwt("tenant-svc")
        payload = pyjwt.decode(
            token, SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert "aud" not in payload

    def test_mint_with_audience_carries_aud(self):
        token = mint_service_jwt("tenant-svc", audience=AUDIENCE)
        payload = pyjwt.decode(token, SECRET, algorithms=["HS256"], audience=AUDIENCE)
        assert payload["aud"] == AUDIENCE == "vkpower-verdict"
        assert payload["tenant_id"] == "tenant-svc"

    def test_mint_blank_audience_is_no_aud(self):
        token = mint_service_jwt("tenant-svc", audience="   ")
        payload = pyjwt.decode(
            token, SECRET, algorithms=["HS256"], options={"verify_aud": False}
        )
        assert "aud" not in payload


# ═══════════════════════════════════════════════════════════════════════
# 4. JWT audience — verify side (_decode_token)
# ═══════════════════════════════════════════════════════════════════════


class TestDecodeAudience:
    def test_roundtrip_accepts_right_aud(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = mint_service_jwt("t-round", audience=settings.qec_jwt_audience)
        ctx = _decode_token(token)
        assert ctx["tenant_id"] == "t-round"
        assert ctx["role"] == "manager"

    def test_foreign_aud_is_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _raw_token(aud="vkpower")  # a foreign (e.g. VKPower) audience
        with pytest.raises(Exception) as ei:
            _decode_token(token)
        assert getattr(ei.value, "status_code", None) == 401
        assert "audience" in str(getattr(ei.value, "detail", "")).lower()

    def test_missing_aud_accepted_in_transition(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _raw_token(aud=None)  # today's tokens carry no aud
        ctx = _decode_token(token)
        assert ctx["tenant_id"] == "tenant-a"

    def test_missing_aud_rejected_when_require_aud(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", True)
        token = _raw_token(aud=None)
        with pytest.raises(Exception) as ei:
            _decode_token(token)
        assert getattr(ei.value, "status_code", None) == 401

    def test_require_aud_still_accepts_correct_aud(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", True)
        token = _raw_token(aud=AUDIENCE)
        assert _decode_token(token)["tenant_id"] == "tenant-a"

    def test_require_aud_still_rejects_foreign_aud(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", True)
        with pytest.raises(Exception) as ei:
            _decode_token(_raw_token(aud="vkpower"))
        assert getattr(ei.value, "status_code", None) == 401

    def test_configured_audience_override(self, monkeypatch):
        # With a custom QEC_JWT_AUDIENCE, the default 'vkpower-verdict' becomes
        # foreign and is rejected; the custom one is accepted.
        monkeypatch.setattr(settings, "qec_jwt_audience", "verdict-eu")
        monkeypatch.setattr(settings, "qec_require_aud", False)
        assert _decode_token(_raw_token(aud="verdict-eu"))["tenant_id"] == "tenant-a"
        with pytest.raises(Exception) as ei:
            _decode_token(_raw_token(aud=AUDIENCE))
        assert getattr(ei.value, "status_code", None) == 401

    def test_empty_audience_opts_gate_out(self, monkeypatch):
        # QEC_JWT_AUDIENCE="" disables the gate entirely (pre-Phase-6 posture):
        # even a foreign aud is accepted.
        monkeypatch.setattr(settings, "qec_jwt_audience", "")
        monkeypatch.setattr(settings, "qec_require_aud", False)
        assert _decode_token(_raw_token(aud="anything"))["tenant_id"] == "tenant-a"

    def test_bad_signature_still_401(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        token = _raw_token(aud=AUDIENCE, secret="the-wrong-secret")
        with pytest.raises(Exception) as ei:
            _decode_token(token)
        assert getattr(ei.value, "status_code", None) == 401

    def test_expired_still_401(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        token = _raw_token(aud=AUDIENCE, exp_delta=timedelta(minutes=-5))
        with pytest.raises(Exception) as ei:
            _decode_token(token)
        assert getattr(ei.value, "status_code", None) == 401

    def test_missing_tenant_still_401(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _raw_token(aud=AUDIENCE, tenant_id=None)
        with pytest.raises(Exception) as ei:
            _decode_token(token)
        assert getattr(ei.value, "status_code", None) == 401


class TestAudienceThroughRealMiddleware:
    def test_correct_aud_passes_gate(self, middleware_client, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = mint_service_jwt("t-mw", audience=AUDIENCE)
        r = middleware_client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200
        assert r.json()["tenant_id"] == "t-mw"

    def test_foreign_aud_rejected_at_gate(self, middleware_client, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _raw_token(aud="vkpower")
        r = middleware_client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 401

    def test_legacy_no_aud_still_passes_in_transition(self, middleware_client, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _raw_token(aud=None)
        r = middleware_client.get(
            "/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"}
        )
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# 5. /health KEK canary
# ═══════════════════════════════════════════════════════════════════════


class TestKekHealthStatus:
    def test_block_shape_production_grade(self):
        block, degraded = kek_health_status(
            provider="gcp_kms", envelope_ready=True, is_deployed=True
        )
        assert block == {
            "provider": "gcp_kms",
            "is_production_grade": True,
            "envelope_ready": True,
        }
        assert degraded is False

    def test_local_kek_degrades_when_deployed(self):
        _, degraded = kek_health_status(
            provider="local", envelope_ready=True, is_deployed=True
        )
        assert degraded is True

    def test_envelope_unavailable_degrades_when_deployed(self):
        _, degraded = kek_health_status(
            provider="gcp_kms", envelope_ready=False, is_deployed=True
        )
        assert degraded is True

    def test_local_kek_not_degraded_in_dev(self):
        _, degraded = kek_health_status(
            provider="local", envelope_ready=False, is_deployed=False
        )
        assert degraded is False

    def test_aws_kms_is_production_grade(self):
        block, _ = kek_health_status(
            provider="aws_kms", envelope_ready=True, is_deployed=True
        )
        assert block["is_production_grade"] is True

    def test_blank_provider_defaults_to_local(self):
        block, _ = kek_health_status(
            provider="", envelope_ready=True, is_deployed=False
        )
        assert block["provider"] == "local"
        assert block["is_production_grade"] is False


class TestHealthEndpointKekBlock:
    """Exercise the REAL app.main.health handler (DB deps patched, no network)."""

    def _patched_main(self, monkeypatch):
        import app.main as m

        async def _noop_init():
            return None

        async def _guc_ok():
            return {"ok": True}

        monkeypatch.setattr(m, "init_db", _noop_init)
        monkeypatch.setattr(m, "guc_self_check", _guc_ok)
        monkeypatch.setattr(m, "is_qec_connected", lambda: True)
        monkeypatch.setattr(m, "is_substrate_connected", lambda: True)
        return m

    @staticmethod
    def _request(envelope_ready: bool):
        env = object() if envelope_ready else None
        return types.SimpleNamespace(
            app=types.SimpleNamespace(state=types.SimpleNamespace(envelope_service=env))
        )

    def test_health_has_kek_block(self, monkeypatch):
        import asyncio

        m = self._patched_main(monkeypatch)
        monkeypatch.setattr(m.settings, "nexus_env", "development")
        monkeypatch.setattr(m.settings, "nexus_kek_provider", "local")
        payload = asyncio.run(m.health(self._request(True)))
        assert "kek" in payload
        assert payload["kek"]["provider"] == "local"
        assert payload["kek"]["is_production_grade"] is False
        # dev env: local KEK does NOT degrade
        assert payload["status"] == "healthy"

    def test_health_degraded_on_local_kek_in_production(self, monkeypatch):
        import asyncio

        m = self._patched_main(monkeypatch)
        monkeypatch.setattr(m.settings, "nexus_env", "production")
        monkeypatch.setattr(m.settings, "nexus_kek_provider", "local")
        payload = asyncio.run(m.health(self._request(True)))
        assert payload["status"] == "degraded"
        assert payload["kek"]["is_production_grade"] is False

    def test_health_degraded_on_missing_envelope_in_production(self, monkeypatch):
        import asyncio

        m = self._patched_main(monkeypatch)
        monkeypatch.setattr(m.settings, "nexus_env", "production")
        monkeypatch.setattr(m.settings, "nexus_kek_provider", "gcp_kms")
        payload = asyncio.run(m.health(self._request(False)))
        assert payload["status"] == "degraded"
        assert payload["kek"]["envelope_ready"] is False

    def test_health_healthy_on_kms_in_production(self, monkeypatch):
        import asyncio

        m = self._patched_main(monkeypatch)
        monkeypatch.setattr(m.settings, "nexus_env", "production")
        monkeypatch.setattr(m.settings, "nexus_kek_provider", "gcp_kms")
        payload = asyncio.run(m.health(self._request(True)))
        assert payload["status"] == "healthy"
        assert payload["kek"]["is_production_grade"] is True

    def test_health_never_leaks_secret(self, monkeypatch):
        import asyncio

        m = self._patched_main(monkeypatch)
        monkeypatch.setattr(m.settings, "nexus_env", "production")
        monkeypatch.setattr(m.settings, "nexus_kek_provider", "gcp_kms")
        payload = asyncio.run(m.health(self._request(True)))
        # The KEK block exposes only the provider NAME + booleans.
        assert set(payload["kek"].keys()) == {
            "provider", "is_production_grade", "envelope_ready",
        }


# ═══════════════════════════════════════════════════════════════════════
# 6. Boot gate is WIRED into main.lifespan (fail-closed exit)
# ═══════════════════════════════════════════════════════════════════════


class TestLifespanBootGate:
    def test_lifespan_refuses_deployed_boot_with_dev_defaults(self, monkeypatch):
        """The gate runs FIRST in lifespan — it raises before any DB work."""
        import asyncio

        import app.main as m

        # Safety net: if the gate were (wrongly) bypassed, init_db must not hit a
        # real DB — this async no-op makes such a regression fail fast, not hang.
        async def _guard_init():
            raise AssertionError("init_db reached — boot gate did not fire first")

        monkeypatch.setattr(m, "init_db", _guard_init)
        monkeypatch.setattr(m.settings, "nexus_env", "production")
        monkeypatch.setattr(m.settings, "nexus_kek_provider", "local")
        monkeypatch.setattr(m.settings, "nexus_jwt_secret", "dev-jwt-secret-change-me")
        monkeypatch.setattr(m.settings, "qec_explorer_token", "")

        async def _enter():
            async with m.lifespan(m.app):
                pass

        with pytest.raises(BootSafetyError):
            asyncio.run(_enter())
