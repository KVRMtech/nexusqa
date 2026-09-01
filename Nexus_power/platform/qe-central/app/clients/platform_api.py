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


#: Provider-REPORTED token usage for one LLM call (M0.6 / T-OB-03).
#:
#: Every field defaults to ``None`` meaning "the provider did not report this",
#: which is deliberately NOT the same as ``0``.  A silent regression in provider
#: reporting must look like a gap in the account, never like a free call.
class LLMUsage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    provider: str = ""
    model: str = ""
    #: Router-performed retries for this response. Every attempt is billed, so
    #: the caller needs it to reconcile spend against logical call count.
    retries: int = 0
    latency_ms: int = 0

    @property
    def reported(self) -> bool:
        """True when the provider reported at least one token count."""
        return self.prompt_tokens is not None or self.completion_tokens is not None

    def as_dict(self) -> dict:
        data = self.model_dump()
        # Derive the total when the boundary did not (older platform-api).
        if data.get("total_tokens") is None and self.reported:
            data["total_tokens"] = int(self.prompt_tokens or 0) + int(
                self.completion_tokens or 0)
        return data


def _mint_or_refuse(tenant_id: str, site: str) -> "tuple[str, LLMResult | None]":
    """Mint the service token, or return an honest ``ok=False`` result.

    ``mint_service_jwt`` refuses to issue a token under an unconfigured or
    known-development signing secret (M0.5 T-SEC-01).  That refusal is correct,
    but these two functions are documented as NEVER raising — every caller
    treats them as best-effort and falls back to a deterministic floor — so a
    misconfigured deployment must degrade here rather than raise into a running
    crawl.  The category is logged, so the cause is visible to an operator.
    """
    try:
        return mint_service_jwt(tenant_id), None
    except ValueError as exc:
        logger.error("qec.platform_api.service_token_refused site=%s reason=%s",
                     site, str(exc)[:200])
        return "", LLMResult(
            ok=False, status_code=0,
            detail="service authentication is not configured"[:300])


def _assert_egress_clean(site: str, *parts: str) -> "LLMResult | None":
    """PII-scan an outbound model payload; return a BLOCKED result, or ``None``.

    M0.5 T-SEC-12 — THE ONE CHOKEPOINT.
    ===================================
    The guard used to be invoked by exactly one caller
    (``routers/apps`` → ``guard_inventory``), while ten other ``complete_llm``
    sites and both screenshot endpoints reached the model with no scan at all.
    A guard that each caller must remember to invoke is not a guard: it is a
    convention, and the ten unguarded sites are what a convention looks like
    after a year.

    So the check moved to the WIRE.  ``complete_llm`` and ``complete_vision``
    are the only two functions in this service that talk to a model, and both
    scan here, immediately before the request is built.  There is no second
    route: a new caller inherits the guard by construction, and a caller that
    wants to skip it would have to write its own HTTP client — which the
    security suite asserts nobody has.

    Returns ``None`` when the payload is clean.  On a block it returns an
    ``ok=False`` :class:`LLMResult`, which is the SAME shape every caller
    already handles for "the model was unavailable" — so a refusal degrades to
    the deterministic floor instead of raising into a crawl.
    """
    from ..services.pii_egress_guard import guard_text

    verdict = guard_text("\n".join(p for p in parts if p), site=site)
    if verdict["safe"]:
        return None
    logger.warning(
        "qec.platform_api.egress_blocked site=%s patterns=%s",
        site, verdict.get("matches"),
    )
    return LLMResult(
        ok=False,
        status_code=0,
        detail=f"egress blocked: {verdict['reason']}"[:300],
    )


def _assert_image_egress_clean(site: str, screenshot_b64: str,
                               redaction=None) -> "LLMResult | None":
    """Gate a SCREENSHOT's egress; return a blocked result, or ``None``.

    Companion to :func:`_assert_egress_clean` for the payload a text scan cannot
    reach.  Same refusal shape, so a block degrades to the deterministic ladder
    the crawler already falls back to rather than raising into a crawl.

    M3.1 / T-VIS-05 — ``redaction`` is the receipt the explorer produced when it
    masked the image.  It defaults to ``None``, which BLOCKS: a caller that does
    not pass one cannot send a screenshot, so the protection cannot be lost by a
    future call site forgetting about it.
    """
    from ..services.pii_egress_guard import guard_image

    verdict = guard_image(screenshot_b64, site=site, nexus_env=settings.nexus_env,
                          redaction=redaction)
    if verdict["safe"]:
        return None
    logger.warning(
        "qec.platform_api.image_egress_blocked site=%s reason=%s patterns=%s",
        site, verdict["reason"][:120], verdict.get("matches"),
    )
    return LLMResult(
        ok=False, status_code=0,
        detail=f"screenshot egress blocked: {verdict['reason']}"[:300],
    )


def _parse_usage(response: httpx.Response, body: dict | None) -> LLMUsage:
    """Read token usage off an LLM response — body first, then headers.

    Two channels because usage must survive BOTH outcomes: a 200 carries it in
    ``body["usage"]``, while a 502 (a provider that billed us and then failed)
    has only the headers platform-api stamps on the error.  Reading both here
    means no caller has to know which path it is on.  Unparseable values are
    left as ``None`` rather than guessed.
    """
    def _int(value) -> int | None:
        try:
            if value is None or isinstance(value, bool):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    raw = (body or {}).get("usage")
    if isinstance(raw, dict):
        return LLMUsage(
            prompt_tokens=_int(raw.get("prompt_tokens")),
            completion_tokens=_int(raw.get("completion_tokens")),
            total_tokens=_int(raw.get("total_tokens")),
            cache_read_tokens=_int(raw.get("cache_read_tokens")),
            cache_creation_tokens=_int(raw.get("cache_creation_tokens")),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            retries=_int(raw.get("retries")) or 0,
            latency_ms=_int(raw.get("latency_ms")) or 0,
        )
    headers = response.headers if response is not None else {}
    return LLMUsage(
        prompt_tokens=_int(headers.get("X-LLM-Prompt-Tokens")),
        completion_tokens=_int(headers.get("X-LLM-Completion-Tokens")),
        total_tokens=_int(headers.get("X-LLM-Total-Tokens")),
        provider=str(headers.get("X-LLM-Provider") or ""),
        model=str(headers.get("X-LLM-Model") or ""),
    )


def _log_usage(tenant_id: str, endpoint: str, usage: "LLMUsage",
               *, outcome: str) -> None:
    """Record one LLM call's spend to the metrics registry and the log.

    Two destinations by design (§10): Prometheus gets the AGGREGATE by bounded
    provider/model/outcome, and the structured log gets the per-call detail
    correlated by the request id the middleware bound — so "how many tokens this
    week" and "which crawl spent them" are both answerable without a
    ``crawl_id`` label ever entering the TSDB.  Never raises.
    """
    try:
        from ..observability import metrics as qec_metrics

        qec_metrics.record_llm_call(
            endpoint=endpoint,
            provider=usage.provider,
            model=usage.model,
            outcome=outcome,
            prompt_tokens=usage.prompt_tokens or 0,
            completion_tokens=usage.completion_tokens or 0,
            cache_read_tokens=usage.cache_read_tokens or 0,
            cache_creation_tokens=usage.cache_creation_tokens or 0,
        )
    except Exception:  # metrics are never load-bearing
        logger.debug("qec.platform_api.llm_metrics_failed", exc_info=True)
    try:
        # No prompt, no completion text, no tenant secret — identifiers, counts
        # and a bounded outcome only.
        logger.info("qec.llm.usage", extra={
            "tenant_id": tenant_id, "endpoint": endpoint, "outcome": outcome,
            "provider": usage.provider, "model": usage.model,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.as_dict().get("total_tokens"),
            "retries": usage.retries, "latency_ms": usage.latency_ms,
            "usage_reported": usage.reported,
        })
    except Exception:
        logger.debug("qec.platform_api.llm_usage_log_failed", exc_info=True)


#: Result of an LLM completion relay (ANSWERS P2). ``ok=False`` ⇒ no usable text →
#: the brief compiler degrades to manual authoring, never a fabricated contract.
class LLMResult(BaseModel):
    ok: bool
    text: str = ""
    status_code: int = 0
    detail: str = ""
    #: Provider-reported spend for this call. Populated on success AND on a
    #: provider error that still billed — an unusable answer is not a free one.
    usage: LLMUsage = LLMUsage()


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
    blocked = _assert_egress_clean(f"llm:{task}", prompt, system)
    if blocked is not None:
        return blocked
    token, refused = _mint_or_refuse(tenant_id, f"llm:{task}")
    if refused is not None:
        return refused
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
        body = {}
        try:
            body = response.json() or {}
        except Exception:
            body = {}
        text = str(body.get("text") or "")
        usage = _parse_usage(response, body)
        _log_usage(tenant_id, "complete", usage, outcome="success")
        return LLMResult(ok=bool(text.strip()), text=text, status_code=200,
                         usage=usage)
    detail = ""
    body = None
    try:
        body = response.json()
        detail = str((body or {}).get("detail") or "")
    except Exception:
        detail = response.text[:300]
    # A provider that billed us and THEN failed still spent tokens. platform-api
    # stamps them on the error headers precisely so this path can keep them —
    # dropping them here would understate spend exactly when it is anomalous.
    usage = _parse_usage(response, body if isinstance(body, dict) else None)
    _log_usage(tenant_id, "complete", usage,
               outcome="error_with_usage" if usage.reported else "error")
    logger.warning("qec.platform_api.llm_rejected",
                   extra={"tenant_id": tenant_id, "status_code": response.status_code, "detail": detail[:300]})
    return LLMResult(ok=False, status_code=response.status_code,
                     detail=detail[:300], usage=usage)


async def complete_vision(
    *, tenant_id: str, prompt: str, screenshot_b64: str, system: str = "",
    max_tokens: int = 800, temperature: float = 0.1, task: str = "vision_medic",
    redaction=None,
) -> LLMResult:
    """Run ONE multimodal LLM completion (text + image).

    Calls ``POST /api/v1/llm/vision`` on platform-api.  The screenshot is
    sent as a base64-encoded PNG in the ``image`` field.  Same resilience
    contract as ``complete_llm``: NEVER raises — ``ok=False`` on any failure.
    """
    if not prompt.strip() or not screenshot_b64:
        return LLMResult(ok=False, detail="empty prompt or screenshot")
    blocked = _assert_egress_clean(f"vision:{task}", prompt, system)
    if blocked is not None:
        return blocked
    # The IMAGE is a separate egress decision from the text beside it: it is the
    # highest-PII payload this system sends, and no text scan can see a rendered
    # SSN. Container validated, metadata scanned, REDACTION PROVEN against these
    # exact bytes, pixels acknowledged — never claimed as scanned.
    # (M0.5 T-SEC-12 pixel half; M3.1 T-VIS-05 redaction half.)
    blocked = _assert_image_egress_clean(f"vision:{task}", screenshot_b64,
                                         redaction=redaction)
    if blocked is not None:
        return blocked
    token, refused = _mint_or_refuse(tenant_id, f"vision:{task}")
    if refused is not None:
        return refused
    body = {"prompt": prompt, "system": system, "image": screenshot_b64,
            "max_tokens": max_tokens, "temperature": temperature, "task": task}
    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=90.0,
        ) as client:
            response = await client.post(
                "/api/v1/llm/vision", json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:
        logger.warning("qec.platform_api.vision_transport_error",
                       extra={"tenant_id": tenant_id, "error": str(exc)[:300]})
        return LLMResult(ok=False, detail=f"transport error: {exc}"[:300])
    if response.status_code == 200:
        body = {}
        try:
            body = response.json() or {}
        except Exception:
            body = {}
        text = str(body.get("text") or "")
        usage = _parse_usage(response, body)
        _log_usage(tenant_id, "vision", usage, outcome="success")
        return LLMResult(ok=bool(text.strip()), text=text, status_code=200,
                         usage=usage)
    detail = ""
    body = None
    try:
        body = response.json()
        detail = str((body or {}).get("detail") or "")
    except Exception:
        detail = response.text[:300]
    usage = _parse_usage(response, body if isinstance(body, dict) else None)
    _log_usage(tenant_id, "vision", usage,
               outcome="error_with_usage" if usage.reported else "error")
    logger.warning("qec.platform_api.vision_rejected",
                   extra={"tenant_id": tenant_id, "status_code": response.status_code, "detail": detail[:300]})
    return LLMResult(ok=False, status_code=response.status_code,
                     detail=detail[:300], usage=usage)


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
    headers = {"Authorization": f"Bearer {token}"}
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
            # IDEMPOTENT PER ARTIFACT. The recording is deliberately kept on the app
            # rather than consumed, because one app yields many artifacts and each
            # needs its own recipe. So the guard against re-minting lives HERE: an
            # artifact that already has a recipe is left alone, and every later crawl
            # still gets one. save_recipe is version-monotonic and supersedes the
            # prior active row, so blind re-posting would churn versions and clear the
            # verified_at that certification reads.
            existing = await client.get(
                f"/api/v1/test-factory/{artifact_id}/recipes", headers=headers,
            )
            if existing.status_code == 200 and (existing.json() or {}).get("count", 0) > 0:
                logger.info(
                    "qec.platform_api.recipe_already_present",
                    extra={"tenant_id": tenant_id, "artifact_id": artifact_id},
                )
                return False
            response = await client.post(
                f"/api/v1/test-factory/{artifact_id}/recipes",
                json=body, headers=headers,
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


async def mirror_environment_profile(
    *,
    tenant_id: str,
    artifact_id: str,
    environment: dict,
) -> bool:
    """Push an onboarding Environment Profile's NON-SECRET routing to the runner's
    governance registry, so the two lists behave as one.

    Onboarding collects the rich profile — base_url, routing cookies/headers, an
    env assertion — while Studio's registry held only posture/production/base_url/
    epoch. Nothing linked them, and an operator reasonably believes they are one
    list. The harm was not cosmetic: ``environment_routing`` copies cookies, headers
    and the env assertion out of the runner-side row into the run context, and that
    row had nowhere to hold them. A cookie-selected lane on a shared host therefore
    landed on the host's default, which for these estates is production.

    Ownership does not move. qe-central remains the owner of the profile; this
    mirrors only what the runner must APPLY, and marks the row ``source=onboarding``
    with a link back, so the panel can say plainly where a row came from.

    SECRETS ARE NOT MIRRORED — the profile's sealed ``creds_blob`` stays here.

    NEVER raises: a failure must not fail the crawl that produced a valid artifact.
    Returns True only when the mirror was actually stored.
    """
    env_id = str((environment or {}).get("environment_id") or "").strip()
    if not env_id or not artifact_id:
        return False

    token = mint_service_jwt(tenant_id)
    headers = {"Authorization": f"Bearer {token}"}
    body = {
        "label": str(environment.get("name") or env_id)[:120],
        "base_url": str(environment.get("base_url") or "")[:2000],
        # Non-secret routing only. `cookies`/`headers` on the profile row are
        # explicitly the non-secret pair; the secret ones live in creds_blob.
        "cookies": list(environment.get("cookies") or []),
        "headers": dict(environment.get("headers") or {}),
        "env_assertion": dict(environment.get("env_assertion") or {}),
        "source": "onboarding",
        "app_env_id": env_id,
        # Posture is NOT asserted here. It is a governance decision that belongs to
        # whoever owns the runner-side registry, and overwriting it from onboarding
        # would silently re-open a production environment somebody had locked.
    }
    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=30.0,
        ) as client:
            response = await client.put(
                f"/api/v1/test-factory/{artifact_id}/environments/{env_id}",
                json=body, headers=headers,
            )
    except Exception as exc:
        logger.warning(
            "qec.platform_api.env_mirror_transport_error",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "environment_id": env_id, "error": str(exc)[:300]},
        )
        return False

    if response.status_code in (200, 201):
        logger.info(
            "qec.platform_api.env_mirrored",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "environment_id": env_id,
                   "cookies": len(body["cookies"]), "headers": len(body["headers"])},
        )
        return True
    logger.warning(
        "qec.platform_api.env_mirror_failed",
        extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
               "environment_id": env_id, "status": response.status_code,
               "detail": response.text[:200]},
    )
    return False


async def fetch_field_resolution(*, tenant_id: str, artifact_id: str,
                                 app_id: str = "") -> dict:
    """Everything a crawl of this application needs in order not to ask twice.

    Two halves under separate keys because they carry entirely different risk:
    ``recalled_values`` is this tenant's own data, decrypted for this one dispatch;
    ``field_priors`` is pooled and value-free. They must never be conflated, so
    they are never merged here.

    T-FE-04 / T-FE-09 · ``app_id`` IS WHAT MAKES ANY OF THIS OUTLIVE ONE CRAWL.
    The artifact is a per-RUN handle and a re-crawl mints a new one, so a memory
    keyed on it expires after exactly one crawl and an identity seeded from it
    gives a DIFFERENT applicant every run.  The application is the thing that
    stays the same, so it is what both are scoped by.

    NEVER raises. Without memory a crawl still runs — it fills what it can and asks
    for the rest, which is exactly the behaviour that existed before any of this.
    """
    # An application with no completed crawl yet has no artifact, and that is no
    # longer a reason to skip: the identity seed and any memory this tenant has
    # already given us for this application are both available without one.
    if not artifact_id and not app_id:
        return {"recalled_values": {}, "field_priors": {}, "identity_seed": ""}
    try:
        token = mint_service_jwt(tenant_id)
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url,
            timeout=phase1_settings.auth_import_timeout_s,
        ) as client:
            resp = await client.get(
                f"/api/v1/test-factory/{artifact_id}/field-resolution",
                params={"app_id": app_id} if app_id else None,
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                logger.info("qec.field_resolution.unavailable status=%s", resp.status_code)
                return {"recalled_values": {}, "field_priors": {}, "identity_seed": ""}
            data = resp.json() or {}
    except Exception as exc:
        logger.info("qec.field_resolution.failed error=%s", str(exc)[:160])
        return {"recalled_values": {}, "field_priors": {}, "identity_seed": ""}

    recalled = data.get("recalled_values")
    priors = data.get("field_priors")
    return {
        "recalled_values": recalled if isinstance(recalled, dict) else {},
        "field_priors": priors if isinstance(priors, dict) else {},
        "identity_seed": str(data.get("identity_seed") or ""),
    }


async def contribute_field_shapes(*, tenant_id: str, artifact_id: str,
                                  verdicts: dict) -> int:
    """Record what a field turned out to be. SHAPES ONLY.

    ``verdicts`` is ``{signature: {type, confidence}}`` from the field agent. There
    is no value in it and no parameter one could be passed through; the receiving
    endpoint forces every type into the closed vocabulary before storing it, and
    drops the whole contribution when the tenant has not opted in.

    NEVER raises: learning is an optimisation, and an optimisation must not be able
    to fail a crawl that already produced a valid artifact."""
    if not artifact_id or not verdicts:
        return 0
    outcomes = [{"signature": sig, "semantic_type": (v or {}).get("type") or "unknown",
                 "accepted": True}
                for sig, v in list(verdicts.items())[:500]
                if isinstance(v, dict) and v.get("type")]
    if not outcomes:
        return 0
    try:
        token = mint_service_jwt(tenant_id)
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url,
            timeout=phase1_settings.auth_import_timeout_s,
        ) as client:
            resp = await client.post(
                f"/api/v1/test-factory/{artifact_id}/field-outcome",
                headers={"Authorization": f"Bearer {token}"},
                json={"outcomes": outcomes},
            )
            if resp.status_code != 200:
                return 0
            return int((resp.json() or {}).get("applied") or 0)
    except Exception as exc:
        logger.info("qec.field_shapes.contribute_failed error=%s", str(exc)[:160])
        return 0
