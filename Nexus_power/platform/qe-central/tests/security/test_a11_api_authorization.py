"""A11.2 / A11.5 — THE API BOUNDARY: who may ask for mutation authority.

The red-team suite next door proves the CRYPTOGRAPHY and the GATES.  This one
proves the thing that sits in front of them, which is where authorisation bugs
actually live: an endpoint wired to the wrong dependency, an anonymous caller
reaching a signing path, a tenant admin reaching a platform-admin write.

THE ONE ASSERTION THAT MATTERS MOST is
:func:`test_a_tenant_admin_cannot_certify_their_own_environment`.  Every
cryptographic control in A11 rests on the claim that
``env_provisioning_records`` is not tenant-writable.  That claim is enforced by
a single dependency on a single route, and a refactor that swapped
``require_platform_admin`` for ``require_role("admin")`` would silently reopen
the entire self-attestation hole while every other test in this milestone stayed
green.  So it is asserted directly, at the boundary.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import require_auth
from app.fleet.rbac import require_platform_admin
from app.routers import attestation

_TENANT_ADMIN = {"sub": "admin@client.test", "tenant_id": "t-1",
                 "email": "admin@client.test", "role": "admin"}
_TENANT_VIEWER = {"sub": "viewer@client.test", "tenant_id": "t-1",
                  "email": "viewer@client.test", "role": "viewer"}
_PLATFORM_ADMIN = {"sub": "ops@nexus.test", "tenant_id": "t-1",
                   "email": "ops@nexus.test", "role": "admin",
                   "platform_admin": True}

_RECORD_BODY = {
    "tenant_id": "t-1", "app_id": "app-1", "environment_id": "env-1",
    "env_kind": "disposable", "target_origin": "https://throwaway.test",
}


def _client(*, user=None, platform_user=None) -> TestClient:
    """A client whose identity is injected at the DEPENDENCY, so each test
    states exactly which principal it is impersonating.

    ``require_auth`` and ``require_platform_admin`` are overridden
    independently — that separation is what lets a test present a TENANT admin
    to a PLATFORM route and observe the real refusal.
    """
    app = FastAPI()
    app.include_router(attestation.router)
    if user is not None:
        app.dependency_overrides[require_auth] = lambda: user
    if platform_user is not None:
        app.dependency_overrides[require_platform_admin] = lambda: platform_user
    return TestClient(app, raise_server_exceptions=False)


# ── JWT authentication is enforced on every route ───────────────────────────

@pytest.mark.parametrize("method,path", [
    ("post", "/api/v1/qec/apps/a/environments/e/provisioning-proof"),
    ("get", "/api/v1/qec/apps/a/environments/e/provisioning-record"),
    ("post", "/api/v1/qec/attestation/revocations"),
    ("get", "/api/v1/qec/attestation/revocations"),
    ("post", "/api/v1/qec/platform/attestation/provisioning-records"),
    ("get", "/api/v1/qec/platform/attestation/keys"),
    ("post", "/api/v1/qec/platform/attestation/keys"),
    ("post", "/api/v1/qec/platform/attestation/keys/kid-1/revoke"),
    ("post", "/api/v1/qec/platform/attestation/keys/rewrap"),
])
def test_every_attestation_route_refuses_an_anonymous_caller(method, path):
    """No route on this router is reachable without a token.

    Enumerated rather than spot-checked: this is the router that mints mutation
    authority, and "we forgot the dependency on the new endpoint" is the most
    ordinary way an API grows a hole.
    """
    client = _client()   # no dependency overrides ⇒ real auth runs
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code in (401, 403), (
        f"{method.upper()} {path} answered {response.status_code} to an "
        f"anonymous caller")


# ── administrator authorization ─────────────────────────────────────────────

def test_a_viewer_cannot_request_a_provisioning_proof():
    """Authenticated is not authorised. A read-only principal must not be able
    to obtain a capability that mutates the customer's application."""
    client = _client(user=_TENANT_VIEWER)
    response = client.post(
        "/api/v1/qec/apps/app-1/environments/env-1/provisioning-proof",
        json={"crawl_id": "crawl-1"})
    assert response.status_code == 403


def test_a_viewer_cannot_revoke():
    """Revocation moves in the fail-closed direction, but it is still an
    operational action with an audit record naming a principal — so it needs a
    principal that is entitled to be named."""
    client = _client(user=_TENANT_VIEWER)
    response = client.post("/api/v1/qec/attestation/revocations",
                           json={"subject_type": "proof", "subject_id": "p-1"})
    assert response.status_code == 403


def _real_token(*, role: str = "admin", platform_admin: bool = False) -> str:
    """A GENUINE signed principal JWT, minted by the service's own minter.

    The platform-admin gate reads the raw token to find the ``platform_admin``
    claim, so it cannot be exercised with a dependency override — an override
    would prove only that the override works. These tests therefore drive the
    endpoints with real Bearer tokens, which is the only way to observe the
    actual RBAC decision.
    """
    from app.config import settings
    from app.fleet.rbac import _mint_jwt

    token, _ = _mint_jwt(
        tenant_id="t-1", email="user@client.test", role=role, ttl_seconds=300,
        audience=settings.qec_jwt_audience, platform_admin=platform_admin)
    return token


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _raw_client() -> TestClient:
    """No dependency overrides at all — real authentication, real RBAC."""
    app = FastAPI()
    app.include_router(attestation.router)
    return TestClient(app, raise_server_exceptions=False)


def test_a_tenant_admin_cannot_certify_their_own_environment():
    """THE LOAD-BEARING AUTHORISATION ASSERTION OF THE WHOLE MILESTONE.

    A tenant admin is a full admin of their own tenant. They may ask for proofs,
    they may revoke, they may read their certification. They may NOT create the
    certification, because that is the platform's finding about their
    environment and the entire trust chain is arithmetic on it.

    Driven with a REAL tenant-admin token so the genuine
    ``require_platform_admin`` gate makes the decision. A refactor swapping this
    route's dependency for ``require_role("admin")`` would reopen the whole
    self-attestation hole while every other A11 test stayed green — this is the
    test that would go red.
    """
    response = _raw_client().post(
        "/api/v1/qec/platform/attestation/provisioning-records",
        json=_RECORD_BODY, headers=_auth(_real_token()))
    assert response.status_code == 403
    assert "platform" in response.text.lower()


def test_a_platform_admin_token_does_reach_the_certification_route():
    """The other side of the same gate.

    Without this, the test above would also pass on a route that refuses
    everybody — which certifies nothing. A platform-admin token gets PAST
    authorisation (and then fails on the absent database, which is the expected
    next step in a unit environment).
    """
    response = _raw_client().post(
        "/api/v1/qec/platform/attestation/provisioning-records",
        json=_RECORD_BODY,
        headers=_auth(_real_token(platform_admin=True)))
    assert response.status_code != 403, (
        "a platform admin was refused by the RBAC gate — the certification "
        "route would be unusable")


def test_a_tenant_admin_cannot_touch_the_issuer_key():
    """The root of trust is not tenant-operable — not creating, not rotating,
    not revoking, not re-wrapping, not even listing."""
    client = _raw_client()
    token = _real_token()
    for method, path, body in (
        ("post", "/api/v1/qec/platform/attestation/keys",
         {"issuer": "qe-central-platform"}),
        ("post", "/api/v1/qec/platform/attestation/keys/kid-1/revoke", {}),
        ("post", "/api/v1/qec/platform/attestation/keys/rewrap", {}),
        ("get", "/api/v1/qec/platform/attestation/keys", None),
    ):
        kwargs = {"json": body} if body is not None else {}
        response = getattr(client, method)(path, headers=_auth(token), **kwargs)
        assert response.status_code == 403, f"{method.upper()} {path}"


def test_a_viewer_token_cannot_reach_the_platform_routes_either():
    """Defence in depth on the role check that precedes the marker check."""
    response = _raw_client().get(
        "/api/v1/qec/platform/attestation/keys",
        headers=_auth(_real_token(role="viewer")))
    assert response.status_code == 403


# ── the request body cannot smuggle a trust decision ────────────────────────

@pytest.mark.parametrize("smuggled", [
    {"crawl_id": "c-1", "env_kind": "disposable"},
    {"crawl_id": "c-1", "target_origin": "https://production.test"},
    {"crawl_id": "c-1", "attestation": {"env_kind": "disposable"}},
    {"crawl_id": "c-1", "provisioning_record": {"env_kind": "disposable"}},
    {"crawl_id": "c-1", "walk_attested": True},
])
def test_the_proof_request_rejects_every_unknown_field(smuggled):
    """``extra='forbid'`` proved at the wire, not just asserted in a docstring.

    None of these fields is read today. The risk is a future refactor that
    starts reading one — at which point a caller who has been quietly sending it
    all along would be self-attesting. A 422 today makes that impossible to
    introduce accidentally.
    """
    client = _client(user=_TENANT_ADMIN)
    response = client.post(
        "/api/v1/qec/apps/app-1/environments/env-1/provisioning-proof",
        json=smuggled)
    assert response.status_code == 422


def test_a_proof_request_without_a_crawl_id_is_refused():
    """A proof not bound to a crawl would be a reusable mutation capability."""
    client = _client(user=_TENANT_ADMIN)
    response = client.post(
        "/api/v1/qec/apps/app-1/environments/env-1/provisioning-proof", json={})
    assert response.status_code == 422


def test_the_certification_body_rejects_unknown_fields():
    """Even at platform-admin level: a field nobody reads is a field somebody
    will eventually start reading."""
    client = _client(user=_PLATFORM_ADMIN, platform_user=_PLATFORM_ADMIN)
    response = client.post(
        "/api/v1/qec/platform/attestation/provisioning-records",
        json={**_RECORD_BODY, "walk_attested": True})
    assert response.status_code == 422


def test_a_certification_with_an_unusable_origin_is_refused():
    """The verifier treats an empty origin as a MISMATCH, never a wildcard, so a
    record pinning one could never authorise anything. Refused at write time
    with a message naming the real cause rather than stored to fail later."""
    client = _client(user=_PLATFORM_ADMIN, platform_user=_PLATFORM_ADMIN)
    response = client.post(
        "/api/v1/qec/platform/attestation/provisioning-records",
        json={**_RECORD_BODY, "target_origin": "not a url"})
    assert response.status_code == 422
    assert "origin" in response.text.lower()


def test_the_certification_ttl_is_bounded():
    """A certification that never expires is a certification nobody revisits."""
    client = _client(user=_PLATFORM_ADMIN, platform_user=_PLATFORM_ADMIN)
    response = client.post(
        "/api/v1/qec/platform/attestation/provisioning-records",
        json={**_RECORD_BODY, "ttl_days": 100_000})
    assert response.status_code == 422


@pytest.mark.parametrize("budget", [-1, 11, 999])
def test_the_certified_mutation_budget_is_bounded_at_the_wire(budget):
    """The verifier's ``HARD_MAX_MUTATIONS_PER_STEP`` is 10. A record above it
    could never be honoured, so it is refused where it is written."""
    client = _client(user=_PLATFORM_ADMIN, platform_user=_PLATFORM_ADMIN)
    response = client.post(
        "/api/v1/qec/platform/attestation/provisioning-records",
        json={**_RECORD_BODY, "max_walk_mutations_per_step": budget})
    assert response.status_code == 422


# ── issuance is rate limited independently of the global limiter ────────────

def test_proof_issuance_has_its_own_rate_limiter_enabled_by_default():
    """Issuance performs a KMS decrypt per call. The GLOBAL API limiter is
    default-OFF, so "there is a limiter somewhere" is not a control this path
    may rely on — an unbounded signing endpoint is a billable DoS against the
    platform's own root of trust."""
    assert attestation._ISSUE_LIMITER.enabled is True
    assert attestation._ISSUE_LIMITER.rate_per_sec > 0


def test_the_rate_limiter_actually_refuses_a_burst():
    """Proved by exhausting it, not by reading the config."""
    from app.api_protect import PrincipalRateLimiter

    limiter = PrincipalRateLimiter(rate_per_sec=1.0, burst_factor=2.0)
    admitted = sum(1 for _ in range(50) if limiter.allow("t-1:admin")[0])
    assert admitted < 50, "the limiter admitted an unbounded burst"


# ── the response is a capability, and is treated as one ─────────────────────

def test_the_issuance_route_is_declared_no_store(monkeypatch):
    """A provisioning proof is a bearer capability. It must not be cached by a
    proxy, a browser, or anything else between here and the dispatcher."""
    from app.services import attestation_issuer as issuer_svc

    class _Issued:
        def as_response(self):
            return {"proof_id": "p-1"}

    async def _fake_issue(*a, **kw):
        return _Issued()

    class _Session:
        async def commit(self):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(issuer_svc, "issue_for_crawl", _fake_issue)
    monkeypatch.setattr(attestation, "tenant_scoped_qec_session",
                        lambda tid: _Session())

    app = FastAPI()
    app.include_router(attestation.router)
    app.dependency_overrides[require_auth] = lambda: _TENANT_ADMIN
    app.state.envelope_service = object()
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/qec/apps/app-1/environments/env-1/provisioning-proof",
        json={"crawl_id": "crawl-1"})
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"


def test_issuance_refuses_when_kms_is_unavailable():
    """FAIL-CLOSED, and a 503 rather than a 500: an unavailable KMS is an
    operational state to retry, not a bug to file. And emphatically NOT a
    fallback to an unsealed key — there is no such thing by construction."""
    app = FastAPI()
    app.include_router(attestation.router)
    app.dependency_overrides[require_auth] = lambda: _TENANT_ADMIN
    app.state.envelope_service = None       # KMS down
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post(
        "/api/v1/qec/apps/app-1/environments/env-1/provisioning-proof",
        json={"crawl_id": "crawl-1"})
    assert response.status_code == 503
    assert "kms" in response.text.lower()
