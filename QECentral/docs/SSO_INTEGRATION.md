# VKPower Verdict — SSO / OIDC / SAML Integration (Phase 8)

**Product:** VKPower Verdict (autonomous Centralized QE)
**Service:** `platform/qe-central` (FastAPI, port 8093) · **Monorepo:** `c:/Users/srika/nexusqa/Nexus_power`
**Status:** additive, **default-OFF**. With `QEC_AUTH_PROVIDER` unset, Verdict authenticates exactly as before (first-party HS256 JWT); the existing auth suite is byte-for-byte unchanged.

Regulated and on-prem buyers require enterprise SSO (SAML or OIDC) with MFA enforced at their identity provider. Phase 8 adds a **pluggable authentication-provider seam** in front of the existing JWT auth so a client can front Verdict with **Okta, Azure AD (Entra ID), or Ping** without any change to Verdict's routes, RBAC, or per-tenant Row-Level-Security.

---

## 1. The seam in one picture

```
                         QEC_AUTH_PROVIDER = jwt | oidc | saml   (default: jwt)
                                    │
   inbound /api/* request ──► jwt_auth_middleware ──► auth_providers.authenticate_request()
                                    │                          │
                                    │            get_auth_provider(settings)  ── fail-closed on
                                    │                          │               unknown / mis-config
                                    ▼                          ▼
                          request.state.user  ◄──  provider.authenticate(request) -> Principal
                          {sub, tenant_id,             │            │            │
                           email, role}               jwt         oidc         saml
                                                        │            │            │
                                            _decode_token()   JWKS verify   internal session
                                            (HS256, aud gate)  (RS256 +      token (minted at
                                                               iss + aud)   ACS from assertion)
```

Every provider returns the SAME `Principal`, and `Principal.as_auth_context()` returns the **identical four-key dict** (`sub` / `tenant_id` / `email` / `role`) that the service produces today. Downstream — every route, the `require_role` RBAC gate, and the `nexus.current_tenant_id` RLS GUC — is unchanged. **The tenant scoping is untouched: whichever provider authenticates, `tenant_id` still drives RLS.**

**Module map (what other code / the deploy imports):**

| Symbol | Location | Purpose |
| --- | --- | --- |
| `AuthProvider` (Protocol) | `app/auth_providers/base.py` | `authenticate(request) -> Principal \| None` |
| `Principal` | `app/auth_providers/base.py` | frozen identity; `.as_auth_context()` → 4-key dict |
| `AuthProviderConfigError` | `app/auth_providers/base.py` | unknown/under-configured provider → fail-closed |
| `get_auth_provider(cfg=None)` | `app/auth_providers/__init__.py` | resolve + cache the active provider |
| `authenticate_request(request)` | `app/auth_providers/__init__.py` | dispatcher → 4-key context (what `app.auth` calls) |
| `resolve_principal(request)` | `app/auth_providers/__init__.py` | dispatcher → `Principal` |
| `JwtAuthProvider` | `app/auth_providers/jwt_provider.py` | default; wraps `_decode_token` verbatim |
| `OidcAuthProvider` | `app/auth_providers/oidc_provider.py` | verify IdP ID token via JWKS |
| `SamlAuthProvider` | `app/auth_providers/saml_provider.py` | validate post-login session token |
| `mint_principal_token(...)` | `app/auth_providers/saml_provider.py` | ACS: assertion attributes → internal session JWT |

---

## 2. Provider protocol & Principal shape

```python
@dataclass(frozen=True)
class Principal:
    sub: str
    tenant_id: str          # the RLS scope — always required
    email: str = ""
    role: str = "viewer"
    provider: str = "jwt"   # which seam authenticated (audit only)
    claims: Mapping = {}     # raw verified claims (audit); NOT in the context
    def as_auth_context(self) -> dict:   # -> {"sub","tenant_id","email","role"}

class AuthProvider(Protocol):
    name: str
    def authenticate(self, request: Request) -> Principal | None: ...
```

`authenticate` contract:
- **valid credential** → `Principal`.
- **no credential** → `None` (the dispatcher fail-closes to `401`).
- **present but invalid** → raises `fastapi.HTTPException(401, ...)` with a specific, non-leaking `detail` (same posture as `app.auth`).

Unknown `QEC_AUTH_PROVIDER`, or a provider selected but under-configured, raises `AuthProviderConfigError` at resolve time, which the dispatcher maps to a `401` — the request is **denied**, never allowed through with an unverified identity.

---

## 3. OIDC (Okta / Azure AD / Ping — recommended)

OIDC is the simplest and most robust option: Verdict verifies the IdP-issued **ID token** (a JWT) on every request against the IdP's published **JWKS**. The IdP's private signing key never touches Verdict; only the public keys are fetched (and cached) from `QEC_OIDC_JWKS_URL`.

### 3.1 Verdict-side environment

```bash
QEC_AUTH_PROVIDER=oidc
QEC_OIDC_ISSUER=https://your-org.okta.com            # must equal the token `iss`
QEC_OIDC_JWKS_URL=https://your-org.okta.com/oauth2/v1/keys
QEC_OIDC_AUDIENCE=<verdict-client-id>                # must be in the token `aud`
QEC_OIDC_ALGORITHMS=RS256                            # comma-separated; asymmetric only
QEC_OIDC_TENANT_CLAIM=tenant_id                      # IdP claim -> Verdict RLS tenant
QEC_OIDC_ROLE_CLAIM=role                             # IdP claim -> Verdict role
QEC_OIDC_EMAIL_CLAIM=email
QEC_OIDC_DEFAULT_ROLE=viewer                         # when the role claim is absent
QEC_OIDC_REQUIRED_ACR=                               # optional MFA pin (see §5); empty = off
QEC_OIDC_LEEWAY_SECONDS=60                           # clock-skew tolerance on exp/iat
```

Verdict enforces, per request: **signature** (against the JWKS key matched by the token `kid`), **issuer** = `QEC_OIDC_ISSUER`, **audience** contains `QEC_OIDC_AUDIENCE`, and **`exp`/`iss`/`aud` all present**. Any failure is a `401` with a specific detail (`Invalid token issuer` / `Invalid token audience` / `Invalid or expired token` / `Token missing tenant claim`).

### 3.2 Per-IdP setup

**Okta**
1. Create an **OIDC → Web/SPA app**; set the redirect URI to your Verdict front door.
2. `QEC_OIDC_ISSUER = https://<org>.okta.com` (or the custom Authorization Server issuer `.../oauth2/<id>`); `QEC_OIDC_JWKS_URL = <issuer>/v1/keys`.
3. Add a **claim** on the ID token that carries the tenant (e.g. an Okta group/profile attribute mapped to `tenant_id`) — see §6.
4. Require MFA via an Okta **sign-on / authenticator-enrollment policy**.

**Azure AD (Entra ID)**
1. **App registration**; note the **Application (client) ID** → `QEC_OIDC_AUDIENCE`.
2. `QEC_OIDC_ISSUER = https://login.microsoftonline.com/<tenant-guid>/v2.0`; `QEC_OIDC_JWKS_URL = https://login.microsoftonline.com/<tenant-guid>/discovery/v2.0/keys`.
3. Emit the tenant via an **app-role**, group, or an **optional/extension claim** mapped to `QEC_OIDC_TENANT_CLAIM`.
4. Enforce MFA via a **Conditional Access** policy; optionally emit `acr` and pin it (§5).

**Ping (PingFederate / PingOne)**
1. Create an **OIDC / OAuth** client; `QEC_OIDC_AUDIENCE = <client-id>`.
2. `QEC_OIDC_ISSUER` = the PingFederate base issuer; `QEC_OIDC_JWKS_URL` = the published `jwks_uri` from the discovery document.
3. Add a tenant attribute to the ID-token contract mapped to `QEC_OIDC_TENANT_CLAIM`.
4. Enforce MFA in the Ping authentication policy.

---

## 4. SAML (assertion → session → internal principal token)

SAML is a browser-redirect, signed-XML-assertion protocol. Verdict follows the standard Service-Provider pattern:

```
IdP  ──(SP-initiated redirect)──►  user authenticates + MFA at IdP
IdP  ──(signed SAML assertion, HTTP-POST)──►  Verdict ACS endpoint
ACS  ── validate XML signature + conditions (issuer / audience / NotOnOrAfter / replay)
ACS  ── map assertion attributes ──►  mint internal principal token (Verdict-audience HS256 JWT)
browser ──(Bearer <principal token>)──►  every /api/* request
```

After login the browser holds a **first-party Verdict session token**, so per-request auth is verified by the SAME proven decoder used for the default provider (`_decode_token`, including the Phase-6 audience gate and the fail-closed `tenant_id` rule). `SamlAuthProvider.authenticate` does exactly that.

The SAML-specific work is the **ACS translation**, performed by `SamlAuthProvider.assertion_to_principal_token(name_id, attributes)`:

```python
provider = SamlAuthProvider.from_settings(settings)          # active when QEC_AUTH_PROVIDER=saml
# ...ACS handler has already verified the assertion's XML signature + conditions...
session_token = provider.assertion_to_principal_token(
    name_id=assertion.name_id,                                # -> sub
    attributes=assertion.attributes,                          # {tenant_id, role, email, ...}
)
# hand session_token to the browser (secure cookie / Authorization: Bearer)
```

`mint_principal_token(...)` stamps `iss="vkpower-verdict-saml"` (so SSO sessions are distinguishable in audit) and the Verdict `aud`, and signs with the shared `NEXUS_JWT_SECRET`.

> **Trust boundary — read this.** Verdict does not bundle an XML parser. The **XML-signature verification, `NotOnOrAfter`/`Recipient`/`AudienceRestriction` condition checks, and replay protection MUST be performed by the deployment's ACS handler** (wiring a SAML toolkit such as `python3-saml` against the client's IdP metadata) **before** calling `assertion_to_principal_token`. The seam mints a session token only from an already-validated assertion. This keeps the dependency surface minimal (PyJWT only) and the trust boundary explicit for a security review.

### 4.1 Verdict-side environment

```bash
QEC_AUTH_PROVIDER=saml
QEC_SAML_IDP_ENTITY_ID=https://idp.example.com/saml     # required
QEC_SAML_SP_ENTITY_ID=https://verdict.example.com/sp    # SP metadata / audience restriction
QEC_SAML_ACS_URL=https://verdict.example.com/saml/acs   # where the IdP POSTs the assertion
QEC_SAML_TENANT_ATTRIBUTE=tenant_id                     # assertion attr -> Verdict RLS tenant
QEC_SAML_ROLE_ATTRIBUTE=role
QEC_SAML_EMAIL_ATTRIBUTE=email
QEC_SAML_DEFAULT_ROLE=viewer
QEC_SAML_SESSION_TTL_SECONDS=3600                       # lifetime of the minted session token
```

---

## 5. MFA

**MFA is enforced at the IdP**, not in Verdict — the IdP will not issue an OIDC ID token or a SAML assertion until the user clears their second factor (Okta Verify / Microsoft Authenticator / FIDO2 / etc.). This is the correct control point: Verdict trusts an assertion/token only because the IdP already proved the factors.

For **OIDC**, an optional **defense-in-depth** check is available: set `QEC_OIDC_REQUIRED_ACR` to a comma-separated list of accepted `acr` (Authentication Context Class Reference) values, and Verdict additionally **refuses** any ID token whose `acr` is not in the list — so a token issued without the required authentication strength is rejected even if the IdP mis-configures a policy. Example: `QEC_OIDC_REQUIRED_ACR=http://schemas.openid.net/pape/policies/2007/06/multi-factor,mfa`. Leave empty (default) to rely solely on the IdP.

---

## 6. Tenant mapping (IdP claim → Verdict tenant)

Every Verdict operation is tenant-scoped via the `nexus.current_tenant_id` RLS GUC, so **every principal MUST resolve to a `tenant_id`** — a token/assertion that carries no tenant is rejected `401` (fail-closed, no "default tenant").

- **OIDC:** the claim named by `QEC_OIDC_TENANT_CLAIM` (default `tenant_id`) is copied into `Principal.tenant_id`. Configure the IdP to emit this claim from a group, app-role, or user profile attribute whose value is the Verdict tenant id (the `tenants.tenant_id` in the `qecentral`/`nexus` DB).
- **SAML:** the assertion attribute named by `QEC_SAML_TENANT_ATTRIBUTE` (default `tenant_id`); single-element attribute lists are normalized to a scalar.

**Provisioning contract:** the tenant id emitted by the IdP must already exist as a `tenants` row (created during onboarding). The IdP claim does not create tenants; it selects an existing one. A common pattern is one IdP group per Verdict tenant, group value = the tenant id. Role mapping is analogous via `QEC_OIDC_ROLE_CLAIM` / `QEC_SAML_ROLE_ATTRIBUTE`, falling back to `*_DEFAULT_ROLE` when absent; roles feed the existing `require_role` RBAC (`viewer` / `manager` / `admin`).

---

## 7. On-prem / air-gapped identity story

Regulated buyers typically run Verdict on-prem against their own IdP:

- **OIDC on-prem:** point `QEC_OIDC_JWKS_URL` at the in-cluster IdP (Keycloak, Okta on-prem/agent, ADFS, PingFederate). JWKS fetch is a single east-west call; keys are cached in-process, so there is no per-request network dependency after warm-up and no outbound internet requirement.
- **SAML on-prem:** ADFS / Shibboleth / Keycloak / PingFederate as the IdP; the ACS handler validates assertions against the IdP's metadata, entirely inside the customer network.
- **No secret leaves the box:** OIDC uses the IdP's **public** JWKS only; the SAML session token is signed with the customer-controlled `NEXUS_JWT_SECRET` (KMS-wrapped in a deployed env — see the Phase-6 boot gate). Nothing in this seam calls out to a Verdict-hosted service.
- **Fail-closed under the safety spine:** the Phase-6 boot gate still refuses to start a deployed process wearing dev-default secrets; SSO does not relax it. If `QEC_AUTH_PROVIDER=oidc|saml` is selected but under-configured, the service denies every request (`401`) rather than falling back to a weaker mode.

---

## 8. Backward-compat & rollout

- **Default is `jwt`.** With `QEC_AUTH_PROVIDER` unset, Verdict authenticates exactly as it does today; the OIDC/SAML providers are inert unless explicitly selected **and** configured.
- **Same downstream identity.** Whatever provider authenticates, `request.state.user` is the same four-key context, so routes, RBAC, RLS, and audit are unchanged.
- **Service-to-service traffic is unaffected.** Internal `mint_service_jwt` tokens (qe-central → the unchanged VKPower factory) continue to flow as first-party HS256 JWTs; SSO fronts human sessions, not the service seam.
- **Suggested rollout:** stand up OIDC in staging → verify tenant/role mapping and MFA → flip `QEC_AUTH_PROVIDER=oidc` in production. To roll back, unset it (returns to `jwt`).

---

## 9. Environment variable reference

| Variable | Default | Meaning |
| --- | --- | --- |
| `QEC_AUTH_PROVIDER` | `jwt` | active provider: `jwt` \| `oidc` \| `saml` (unknown ⇒ fail-closed) |
| `QEC_OIDC_ISSUER` | `""` | required for `oidc`: expected token `iss` |
| `QEC_OIDC_JWKS_URL` | `""` | required for `oidc`: IdP public signing keys |
| `QEC_OIDC_AUDIENCE` | `""` | required for `oidc`: expected token `aud` |
| `QEC_OIDC_ALGORITHMS` | `RS256` | comma-separated accepted asymmetric algorithms |
| `QEC_OIDC_TENANT_CLAIM` | `tenant_id` | ID-token claim → Verdict RLS tenant |
| `QEC_OIDC_ROLE_CLAIM` | `role` | ID-token claim → Verdict role |
| `QEC_OIDC_EMAIL_CLAIM` | `email` | ID-token claim → principal email |
| `QEC_OIDC_DEFAULT_ROLE` | `viewer` | role when the role claim is absent |
| `QEC_OIDC_REQUIRED_ACR` | `""` | optional MFA `acr` pin (comma-separated; empty = off) |
| `QEC_OIDC_LEEWAY_SECONDS` | `60` | clock-skew leeway on `exp`/`iat` |
| `QEC_SAML_IDP_ENTITY_ID` | `""` | required for `saml`: IdP EntityID |
| `QEC_SAML_SP_ENTITY_ID` | `""` | Verdict SP EntityID (metadata / audience restriction) |
| `QEC_SAML_ACS_URL` | `""` | Assertion Consumer Service URL |
| `QEC_SAML_TENANT_ATTRIBUTE` | `tenant_id` | assertion attribute → Verdict RLS tenant |
| `QEC_SAML_ROLE_ATTRIBUTE` | `role` | assertion attribute → Verdict role |
| `QEC_SAML_EMAIL_ATTRIBUTE` | `email` | assertion attribute → principal email |
| `QEC_SAML_DEFAULT_ROLE` | `viewer` | role when the role attribute is absent |
| `QEC_SAML_SESSION_TTL_SECONDS` | `3600` | lifetime of the minted internal session token |

All default empty/`jwt` ⇒ today's behavior. See `platform/qe-central/app/config.py` for the pinned aliases and `tests/unit/test_auth_providers.py` for the behavioral contract.
