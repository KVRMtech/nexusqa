"""QE-Central Phase-8 — pluggable auth-provider seam (SSO/OIDC/SAML) tests.

Pins the two things that MUST hold for this additive seam:

  1. Backward-compat: with ``QEC_AUTH_PROVIDER`` unset the active provider is the
     first-party HS256-JWT provider, and its output is BYTE-IDENTICAL to today's
     ``_decode_token`` — existing tokens decode the same, the Phase-6 audience
     rules hold, and the real middleware behaves exactly as before.
  2. The new providers: an OIDC provider validates issuer + audience + signature
     against a stub JWKS and rejects a wrong-issuer / wrong-audience / bad-signature
     token; the SAML seam maps a validated assertion to an internal principal
     token that verifies through the same decoder; an unknown provider is
     fail-closed; and every provider yields the SAME four-key Principal shape.

No network is touched — the OIDC provider is exercised with an injected
signing-key resolver over a locally-generated RSA keypair / stub JWKS.
"""
from __future__ import annotations

import json
import types
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth import _decode_token, jwt_auth_middleware, require_auth
from app.auth_providers import (
    AuthProviderConfigError,
    JwtAuthProvider,
    OidcAuthProvider,
    Principal,
    SamlAuthProvider,
    authenticate_request,
    available_providers,
    get_auth_provider,
    mint_principal_token,
    reset_provider_cache,
    resolve_principal,
)
from app.auth_providers.saml_provider import INTERNAL_PRINCIPAL_ISSUER
from app.config import AUDIENCE, Settings, settings
from app.service_token import mint_service_jwt

SECRET = settings.nexus_jwt_secret


# ─── fakes / helpers ────────────────────────────────────────────────────

def _req(token: str | None = None, *, method: str = "GET", query_token: str | None = None):
    """A minimal Request stand-in for _token_from_request (header/query/method)."""
    headers: dict = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    query = {"token": query_token} if query_token is not None else {}
    return types.SimpleNamespace(headers=headers, method=method, query_params=query)


def _hs256(
    *,
    tenant_id: str | None = "tenant-a",
    role: str = "manager",
    sub: str = "user-x",
    email: str = "user@example.test",
    aud: str | None = None,
    secret: str = SECRET,
    exp_delta: timedelta = timedelta(minutes=10),
) -> str:
    """Encode a first-party HS256 JWT (the shape today's issuers mint)."""
    claims: dict = {
        "sub": sub,
        "email": email,
        "role": role,
        "exp": datetime.now(timezone.utc) + exp_delta,
    }
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    if aud is not None:
        claims["aud"] = aud
    return pyjwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture(autouse=True)
def _clean_provider_cache():
    """Isolate the provider cache across tests that monkeypatch settings."""
    reset_provider_cache()
    yield
    reset_provider_cache()


# ── OIDC stub-JWKS keypair (module-scoped: RSA gen is the slow part) ──

class _Rsa:
    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.priv_pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "test-key-1", "alg": "RS256", "use": "sig"})
        self.jwks = {"keys": [jwk]}


@pytest.fixture(scope="module")
def rsa_a() -> _Rsa:
    return _Rsa()


@pytest.fixture(scope="module")
def rsa_b() -> _Rsa:
    return _Rsa()


def _jwks_resolver(jwks: dict):
    """A stub JWKS resolver mirroring PyJWKClient.get_signing_key_from_jwt."""
    jwk_set = pyjwt.PyJWKSet.from_dict(jwks)

    def _resolve(token: str):
        kid = pyjwt.get_unverified_header(token).get("kid")
        for key in jwk_set.keys:
            if key.key_id == kid:
                return key.key
        raise pyjwt.PyJWKClientError(f"no key for kid={kid!r}")

    return _resolve


ISSUER = "https://idp.example.test"
OIDC_AUD = "verdict-client-id"


def _oidc_token(
    rsa_key: _Rsa,
    *,
    iss: str = ISSUER,
    aud: str = OIDC_AUD,
    tenant_id: str | None = "tenant-oidc",
    role: str | None = "manager",
    sub: str = "okta|u1",
    email: str = "u1@example.test",
    acr: str | None = None,
    exp_delta: timedelta = timedelta(minutes=10),
    extra: dict | None = None,
) -> str:
    claims: dict = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "email": email,
        "exp": datetime.now(timezone.utc) + exp_delta,
        "iat": datetime.now(timezone.utc),
    }
    if tenant_id is not None:
        claims["tenant_id"] = tenant_id
    if role is not None:
        claims["role"] = role
    if acr is not None:
        claims["acr"] = acr
    if extra:
        claims.update(extra)
    return pyjwt.encode(
        claims, rsa_key.priv_pem, algorithm="RS256", headers={"kid": "test-key-1"},
    )


def _oidc_provider(rsa_key: _Rsa, **overrides) -> OidcAuthProvider:
    params = dict(
        issuer=ISSUER,
        audience=OIDC_AUD,
        jwks_url="https://idp.example.test/jwks",  # unused (resolver injected)
        signing_key_resolver=_jwks_resolver(rsa_key.jwks),
    )
    params.update(overrides)
    return OidcAuthProvider(**params)


# ═══════════════════════════════════════════════════════════════════════
# 1. Default jwt provider is BYTE-IDENTICAL to today's _decode_token
# ═══════════════════════════════════════════════════════════════════════


class TestJwtProviderByteIdentical:
    def test_default_setting_is_jwt(self):
        # QEC_AUTH_PROVIDER unset ⇒ jwt (the pre-Phase-8 posture).
        assert Settings().qec_auth_provider == "jwt"
        assert settings.qec_auth_provider == "jwt"

    def test_existing_token_decodes_identically(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _hs256(tenant_id="t-9", role="manager", sub="u-9", email="e@x.test")
        expected = _decode_token(token)
        got = JwtAuthProvider().authenticate(_req(token)).as_auth_context()
        assert got == expected
        assert got == {
            "sub": "u-9", "tenant_id": "t-9", "email": "e@x.test", "role": "manager",
        }

    def test_service_token_decodes_identically(self):
        token = mint_service_jwt("tenant-svc")
        assert (
            JwtAuthProvider().authenticate(_req(token)).as_auth_context()
            == _decode_token(token)
        )

    def test_dispatcher_default_equals_decode_token(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _hs256(tenant_id="t-disp")
        assert authenticate_request(_req(token)) == _decode_token(token)

    def test_missing_token_is_401(self):
        with pytest.raises(HTTPException) as ei:
            JwtAuthProvider().authenticate(_req(None))
        assert ei.value.status_code == 401

    def test_bad_signature_is_401(self):
        token = _hs256(secret="the-wrong-secret")
        with pytest.raises(HTTPException) as ei:
            JwtAuthProvider().authenticate(_req(token))
        assert ei.value.status_code == 401

    def test_missing_tenant_is_401(self):
        with pytest.raises(HTTPException) as ei:
            JwtAuthProvider().authenticate(_req(_hs256(tenant_id=None)))
        assert ei.value.status_code == 401

    def test_phase6_foreign_aud_still_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        with pytest.raises(HTTPException) as ei:
            JwtAuthProvider().authenticate(_req(_hs256(aud="vkpower")))
        assert ei.value.status_code == 401

    def test_phase6_missing_aud_transition_accepted(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        ctx = JwtAuthProvider().authenticate(_req(_hs256(aud=None))).as_auth_context()
        assert ctx["tenant_id"] == "tenant-a"


# ═══════════════════════════════════════════════════════════════════════
# 2. Real middleware + require_auth are unchanged under the default provider
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.middleware("http")(jwt_auth_middleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/qec/whoami")
    async def whoami(user: dict = Depends(require_auth)):
        return user

    return TestClient(app)


class TestMiddlewareUnchanged:
    def test_valid_token_200_and_context(self, client, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _hs256(sub="u-1", tenant_id="t-1", email="e@x.test", role="manager")
        r = client.get("/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json() == {
            "sub": "u-1", "tenant_id": "t-1", "email": "e@x.test", "role": "manager",
        }

    def test_missing_token_401(self, client):
        assert client.get("/api/v1/qec/whoami").status_code == 401

    def test_public_health_needs_no_token(self, client):
        assert client.get("/health").status_code == 200

    def test_query_token_on_get_allowed(self, client, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _hs256()
        assert client.get(f"/api/v1/qec/whoami?token={token}").status_code == 200


# ═══════════════════════════════════════════════════════════════════════
# 3. OIDC provider — validate issuer + audience + signature (stub JWKS)
# ═══════════════════════════════════════════════════════════════════════


class TestOidcProvider:
    def test_valid_token_maps_to_principal(self, rsa_a):
        p = _oidc_provider(rsa_a)
        principal = p.authenticate(_req(_oidc_token(rsa_a)))
        assert isinstance(principal, Principal)
        assert principal.tenant_id == "tenant-oidc"
        assert principal.role == "manager"
        assert principal.sub == "okta|u1"
        assert principal.provider == "oidc"
        assert principal.as_auth_context() == {
            "sub": "okta|u1",
            "tenant_id": "tenant-oidc",
            "email": "u1@example.test",
            "role": "manager",
        }

    def test_wrong_issuer_rejected(self, rsa_a):
        p = _oidc_provider(rsa_a)
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(_oidc_token(rsa_a, iss="https://evil.example.test")))
        assert ei.value.status_code == 401
        assert "issuer" in str(ei.value.detail).lower()

    def test_wrong_audience_rejected(self, rsa_a):
        p = _oidc_provider(rsa_a)
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(_oidc_token(rsa_a, aud="some-other-client")))
        assert ei.value.status_code == 401
        assert "audience" in str(ei.value.detail).lower()

    def test_bad_signature_rejected(self, rsa_a, rsa_b):
        # Token signed by key B but the provider only knows key A's JWKS.
        p = _oidc_provider(rsa_a)
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(_oidc_token(rsa_b)))
        assert ei.value.status_code == 401

    def test_expired_token_rejected(self, rsa_a):
        p = _oidc_provider(rsa_a)
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(_oidc_token(rsa_a, exp_delta=timedelta(minutes=-5))))
        assert ei.value.status_code == 401

    def test_missing_tenant_claim_rejected(self, rsa_a):
        p = _oidc_provider(rsa_a)
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(_oidc_token(rsa_a, tenant_id=None)))
        assert ei.value.status_code == 401
        assert "tenant" in str(ei.value.detail).lower()

    def test_missing_token_rejected(self, rsa_a):
        p = _oidc_provider(rsa_a)
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(None))
        assert ei.value.status_code == 401

    def test_default_role_applied_when_role_claim_absent(self, rsa_a):
        p = _oidc_provider(rsa_a, default_role="viewer")
        principal = p.authenticate(_req(_oidc_token(rsa_a, role=None)))
        assert principal.role == "viewer"

    def test_custom_tenant_claim_mapping(self, rsa_a):
        p = _oidc_provider(rsa_a, tenant_claim="org_id")
        token = _oidc_token(rsa_a, tenant_id=None, extra={"org_id": "tenant-42"})
        assert p.authenticate(_req(token)).tenant_id == "tenant-42"

    def test_required_acr_enforced_for_mfa(self, rsa_a):
        p = _oidc_provider(rsa_a, required_acr=("mfa", "phr"))
        # Token without a satisfying acr is rejected...
        with pytest.raises(HTTPException) as ei:
            p.authenticate(_req(_oidc_token(rsa_a, acr="pwd")))
        assert ei.value.status_code == 401
        # ...and accepted once the IdP asserts a required acr.
        principal = p.authenticate(_req(_oidc_token(rsa_a, acr="mfa")))
        assert principal.tenant_id == "tenant-oidc"

    def test_construction_requires_issuer_and_audience(self, rsa_a):
        with pytest.raises(AuthProviderConfigError):
            OidcAuthProvider(issuer="", audience=OIDC_AUD, jwks_url="x")
        with pytest.raises(AuthProviderConfigError):
            OidcAuthProvider(issuer=ISSUER, audience="", jwks_url="x")
        with pytest.raises(AuthProviderConfigError):
            OidcAuthProvider(issuer=ISSUER, audience=OIDC_AUD)  # no jwks/resolver


# ═══════════════════════════════════════════════════════════════════════
# 4. SAML seam — assertion → internal principal token → verify
# ═══════════════════════════════════════════════════════════════════════


class TestSamlProvider:
    def _provider(self) -> SamlAuthProvider:
        return SamlAuthProvider(idp_entity_id="https://idp.example/saml", audience=AUDIENCE)

    def test_assertion_maps_to_principal_token_and_verifies(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        p = self._provider()
        token = p.assertion_to_principal_token(
            name_id="alice@corp.test",
            attributes={
                "tenant_id": ["tenant-saml"],  # SAML values arrive as lists
                "role": ["manager"],
                "email": ["alice@corp.test"],
            },
        )
        principal = p.authenticate(_req(token))
        assert principal.provider == "saml"
        assert principal.as_auth_context() == {
            "sub": "alice@corp.test",
            "tenant_id": "tenant-saml",
            "email": "alice@corp.test",
            "role": "manager",
        }

    def test_minted_principal_token_carries_saml_issuer(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        p = self._provider()
        token = p.assertion_to_principal_token(
            "bob", {"tenant_id": "t-b", "role": "viewer"},
        )
        payload = pyjwt.decode(token, SECRET, algorithms=["HS256"], audience=AUDIENCE)
        assert payload["iss"] == INTERNAL_PRINCIPAL_ISSUER
        assert payload["tenant_id"] == "t-b"

    def test_assertion_without_tenant_is_fail_closed(self):
        p = self._provider()
        with pytest.raises(AuthProviderConfigError):
            p.assertion_to_principal_token("carol", {"role": ["viewer"]})

    def test_default_role_applied(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        p = SamlAuthProvider(
            idp_entity_id="idp", audience=AUDIENCE, default_role="viewer",
        )
        token = p.assertion_to_principal_token("d", {"tenant_id": "t-d"})
        assert p.authenticate(_req(token)).role == "viewer"

    def test_mint_principal_token_requires_tenant_and_audience(self):
        with pytest.raises(ValueError):
            mint_principal_token(sub="x", tenant_id="", audience=AUDIENCE)
        with pytest.raises(ValueError):
            mint_principal_token(sub="x", tenant_id="t", audience="")

    def test_construction_requires_idp_entity_id(self):
        with pytest.raises(AuthProviderConfigError):
            SamlAuthProvider(idp_entity_id="")


# ═══════════════════════════════════════════════════════════════════════
# 5. Registry + dispatcher — selection, fail-closed, consistent shape
# ═══════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_available_providers(self):
        assert available_providers() == ("jwt", "oidc", "saml")

    def test_default_provider_is_jwt(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_auth_provider", "jwt")
        assert isinstance(get_auth_provider(settings), JwtAuthProvider)

    def test_oidc_selected_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_auth_provider", "oidc")
        monkeypatch.setattr(settings, "qec_oidc_issuer", ISSUER)
        monkeypatch.setattr(settings, "qec_oidc_audience", OIDC_AUD)
        monkeypatch.setattr(settings, "qec_oidc_jwks_url", "https://idp/jwks")
        assert isinstance(get_auth_provider(settings), OidcAuthProvider)

    def test_saml_selected_when_configured(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_auth_provider", "saml")
        monkeypatch.setattr(settings, "qec_saml_idp_entity_id", "https://idp/saml")
        assert isinstance(get_auth_provider(settings), SamlAuthProvider)

    def test_unknown_provider_raises(self):
        cfg = types.SimpleNamespace(qec_auth_provider="totally-bogus")
        with pytest.raises(AuthProviderConfigError):
            get_auth_provider(cfg)

    def test_unknown_provider_fail_closed_at_dispatch(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_auth_provider", "totally-bogus")
        with pytest.raises(HTTPException) as ei:
            resolve_principal(_req(_hs256()))
        assert ei.value.status_code == 401

    def test_oidc_selected_but_unconfigured_fail_closed(self, monkeypatch):
        # oidc chosen with no issuer/audience ⇒ builder raises ⇒ dispatcher 401.
        monkeypatch.setattr(settings, "qec_auth_provider", "oidc")
        monkeypatch.setattr(settings, "qec_oidc_issuer", "")
        monkeypatch.setattr(settings, "qec_oidc_audience", "")
        monkeypatch.setattr(settings, "qec_oidc_jwks_url", "")
        with pytest.raises(HTTPException) as ei:
            resolve_principal(_req(_hs256()))
        assert ei.value.status_code == 401

    def test_dispatch_uses_active_jwt_provider(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_auth_provider", "jwt")
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        token = _hs256(tenant_id="t-active")
        assert resolve_principal(_req(token)).tenant_id == "t-active"


class TestOidcThroughRealMiddleware:
    """End-to-end: middleware → registry → OIDC provider → request.state.user.

    Overrides only the registry's oidc BUILDER so the wired path (no network) is
    exercised; the provider itself is the real one over a stub JWKS.
    """

    def test_oidc_token_passes_real_middleware(self, rsa_a, monkeypatch):
        import app.auth_providers as ap

        monkeypatch.setattr(settings, "qec_auth_provider", "oidc")
        monkeypatch.setitem(ap._FACTORIES, "oidc", lambda cfg: _oidc_provider(rsa_a))
        reset_provider_cache()

        app = FastAPI()
        app.middleware("http")(jwt_auth_middleware)

        @app.get("/api/v1/qec/whoami")
        async def whoami(user: dict = Depends(require_auth)):
            return user

        client = TestClient(app)
        token = _oidc_token(rsa_a)
        r = client.get("/api/v1/qec/whoami", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json() == {
            "sub": "okta|u1",
            "tenant_id": "tenant-oidc",
            "email": "u1@example.test",
            "role": "manager",
        }
        # A wrong-issuer token is rejected by the SAME wired path.
        bad = _oidc_token(rsa_a, iss="https://evil.example.test")
        r2 = client.get("/api/v1/qec/whoami", headers={"Authorization": f"Bearer {bad}"})
        assert r2.status_code == 401


class TestPrincipalShapeConsistency:
    """Every provider yields the SAME four-key context, regardless of protocol."""

    _KEYS = {"sub", "tenant_id", "email", "role"}

    def test_jwt_shape(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        monkeypatch.setattr(settings, "qec_require_aud", False)
        ctx = JwtAuthProvider().authenticate(_req(_hs256())).as_auth_context()
        assert set(ctx) == self._KEYS

    def test_oidc_shape(self, rsa_a):
        ctx = _oidc_provider(rsa_a).authenticate(_req(_oidc_token(rsa_a))).as_auth_context()
        assert set(ctx) == self._KEYS

    def test_saml_shape(self, monkeypatch):
        monkeypatch.setattr(settings, "qec_jwt_audience", AUDIENCE)
        p = SamlAuthProvider(idp_entity_id="idp", audience=AUDIENCE)
        token = p.assertion_to_principal_token("u", {"tenant_id": "t", "role": "viewer"})
        ctx = p.authenticate(_req(token)).as_auth_context()
        assert set(ctx) == self._KEYS

    def test_context_excludes_raw_claims(self, rsa_a):
        # Raw claims stay on the Principal for audit but never leak into context.
        principal = _oidc_provider(rsa_a).authenticate(
            _req(_oidc_token(rsa_a, extra={"secret_claim": "x"})),
        )
        assert "secret_claim" in principal.claims
        assert "secret_claim" not in principal.as_auth_context()
