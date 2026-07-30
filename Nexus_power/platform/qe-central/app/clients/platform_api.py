"""QE-Central — typed client for the UNCHANGED VKPower factory (platform-api).

QE-Central consumes the factory over HTTP so every interaction rides the same
audited path a human uses (design §3.1, R-13).  Phase-1 needs exactly ONE
endpoint: the E3 auth-import seam that turns a crawler-relayed Playwright
``storageState`` into this artifact's ENCRYPTED auth profile
(``POST /api/v1/test-factory/{artifact_id}/playwright/auth/import`` —
test_factory.py:3081).

Security posture: the session is encrypted at rest by platform-api's
EnvelopeService (AAD=artifact_id) and NEVER returned; this client only relays.
The endpoint is OFF by default (403 unless ``NEXUS_QEC_AUTH_IMPORT_ENABLED``)
and refuses (503) when encryption is unavailable — so a disabled/mis-provisioned
deployment can never leak or plaintext-store a session.  Every non-2xx is
reported honestly to the caller; the caller decides fatality.
"""
from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel

from ..config import settings
from ..service_token import mint_service_jwt
from .config import phase1_settings

logger = logging.getLogger(__name__)

#: Result of an E3 auth-import relay (honest, never masked).
class AuthImportResult(BaseModel):
    ok: bool
    status_code: int
    detail: str = ""


#: Result of an LLM completion relay (ANSWERS P2). ``ok=False`` ⇒ no usable text →
#: the brief compiler degrades to manual authoring, never a fabricated contract.
class LLMResult(BaseModel):
    ok: bool
    text: str = ""
    status_code: int = 0
    detail: str = ""


async def complete_llm(
    *, tenant_id: str, prompt: str, system: str = "",
    max_tokens: int = 2400, temperature: float = 0.2, task: str = "brief_compile",
) -> LLMResult:
    """Run ONE LLM completion via platform-api's ``/api/v1/llm/complete`` (authoring
    time only). Mints a ``role=manager`` service JWT. NEVER raises — a transport
    error / non-200 returns ``ok=False`` so the caller falls back to manual authoring
    (the grounding gate then simply has nothing to propose)."""
    if not prompt.strip():
        return LLMResult(ok=False, detail="empty prompt")
    token = mint_service_jwt(tenant_id)
    body = {"prompt": prompt, "system": system, "max_tokens": max_tokens,
            "temperature": temperature, "task": task}
    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=60.0,
        ) as client:
            response = await client.post(
                "/api/v1/llm/complete", json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # transport failure — honest, non-fatal
        logger.warning("qec.platform_api.llm_transport_error",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:300]})
        return LLMResult(ok=False, detail=f"transport error: {exc}"[:300])
    if response.status_code == 200:
        try:
            text = str((response.json() or {}).get("text") or "")
        except Exception:
            text = ""
        return LLMResult(ok=bool(text.strip()), text=text, status_code=200)
    detail = ""
    try:
        detail = str(response.json().get("detail") or "")
    except Exception:
        detail = response.text[:300]
    logger.warning("qec.platform_api.llm_rejected",
                   extra={"tenant_id": tenant_id, "status_code": response.status_code, "detail": detail[:300]})
    return LLMResult(ok=False, status_code=response.status_code, detail=detail[:300])


def _auth_import_path(artifact_id: str) -> str:
    return f"/api/v1/test-factory/{artifact_id}/playwright/auth/import"


async def import_auth_profile(
    *,
    tenant_id: str,
    artifact_id: str,
    storage_state: dict,
    label: str | None = None,
) -> AuthImportResult:
    """Relay a Playwright ``storageState`` to platform-api's E3 endpoint.

    Mints a short-lived ``role=manager`` service JWT for ``tenant_id`` (the
    endpoint's RBAC gate), POSTs the state, and returns an honest result.  This
    function NEVER raises for an HTTP/transport error — it returns
    ``ok=False`` with the status/detail so the completion callback can record
    the outcome without discarding a successful substrate write.

    A caller MUST treat ``ok=False`` as "no auth profile stored" (e.g. the
    endpoint is disabled, or encryption is unavailable) — never as success.
    """
    if not storage_state:
        return AuthImportResult(ok=False, status_code=0, detail="empty storage_state")

    token = mint_service_jwt(tenant_id)
    url = _auth_import_path(artifact_id)
    body: dict = {"storage_state": storage_state}
    if label:
        body["label"] = label[:200]

    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url,
            timeout=phase1_settings.auth_import_timeout_s,
        ) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # transport failure — honest, non-fatal to the write
        logger.warning(
            "qec.platform_api.auth_import_transport_error",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "error": str(exc)[:300]},
        )
        return AuthImportResult(ok=False, status_code=0, detail=f"transport error: {exc}"[:300])

    if response.status_code == 200:
        logger.info(
            "qec.platform_api.auth_import_saved",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id},
        )
        return AuthImportResult(ok=True, status_code=200, detail="saved")

    detail = ""
    try:
        detail = str(response.json().get("detail") or "")
    except Exception:
        detail = response.text[:300]
    logger.warning(
        "qec.platform_api.auth_import_rejected",
        extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
               "status_code": response.status_code, "detail": detail[:300]},
    )
    return AuthImportResult(ok=False, status_code=response.status_code, detail=detail[:300])


async def materialise_login_recipe(
    *,
    tenant_id: str,
    artifact_id: str,
    recording: dict,
) -> bool:
    """Turn a login RECORDED at onboarding into a real recipe on its artifact.

    A recipe is artifact-scoped, but a recording happens before any artifact exists
    — so the choreography waits on the app row until the first crawl mints one, and
    this is what completes the journey. Once stored, ANY member with a credential
    card for its slots can replay the login; until then only the recorded session
    works, and only for whoever recorded it.

    The reuse keys are carried through deliberately: without login_type_key and
    login_domain the recipe can never be PROPOSED to the next app on the same host,
    which is the whole point of recording once.

    NEVER raises. A failure here must not fail a crawl that already produced a
    valid, evidence-passing artifact — the crawl's own session already got it in.
    Returns True only when the recipe was actually stored.
    """
    steps = (recording or {}).get("steps") or []
    slots = (recording or {}).get("slots") or []
    if not steps or not slots:
        return False

    token = mint_service_jwt(tenant_id)
    body = {
        "steps": steps,
        "slots": slots,
        "source": "login_recording",
        "login_type_key": str((recording or {}).get("login_type_key") or "")[:64],
        "login_domain": str((recording or {}).get("domain") or "")[:253],
    }
    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=30.0,
        ) as client:
            response = await client.post(
                f"/api/v1/test-factory/{artifact_id}/recipes",
                json=body, headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        logger.warning(
            "qec.platform_api.recipe_materialise_transport_error",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "error": str(exc)[:300]},
        )
        return False

    if response.status_code in (200, 201):
        logger.info(
            "qec.platform_api.recipe_materialised",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "slots": [s.get("name") for s in slots][:10]},
        )
        return True
    logger.warning(
        "qec.platform_api.recipe_materialise_failed",
        extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
               "status": response.status_code, "detail": response.text[:200]},
    )
    return False
