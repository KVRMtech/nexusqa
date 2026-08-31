"""QE-Central — typed client for the UNCHANGED VKPower factory (read + generate).

S4 consumes two more VKPower endpoints over HTTP with a minted ``role=manager``
service JWT (R-13), so every call rides the same audited path a human uses:

  * ``GET  /api/v1/test-factory/{artifact_id}/rtm``      — the read-only
    Requirement→Step→Assertion matrix the tier labeller reads (test_factory.py:
    3691-3712).  Returns 200 with ``tests: []`` when the artifact has no cases.
  * ``POST /api/v1/test-factory/{artifact_id}/generate`` — the materialize
    bridge: compile grounded cases from the artifact's substrate (test_factory.py:
    181-199).

Every non-2xx raises :class:`FactoryClientError` carrying the upstream status +
detail so the router can map it honestly (a 404 artifact stays a 404, an LLM/gate
failure surfaces its reason) — never a silent success.  This module is separate
from ``platform_api.py`` (the E3 auth-import relay) so each VKPower seam is a
small, independently-pinned surface.

Phase-5.5 wiring (additive, default-preserving): the correlation id is propagated
to VKPower so one id spans QE-Central and the factory, the factory-latency metric
is recorded on every call, and the IDEMPOTENT ``/rtm`` read is retried on a
transient blip (:func:`app.clients.resilience.call_with_retries`).  The
NON-idempotent ``/generate`` trigger is executed EXACTLY ONCE — never auto-retried.
"""
from __future__ import annotations

import logging
import time

import httpx

from ..config import settings
from ..observability import outbound_request_id_headers, record_factory_call
from ..service_token import mint_service_jwt
from .resilience import call_with_retries

logger = logging.getLogger(__name__)

#: Read timeout for the factory calls (generate compiles synchronously).
_RTM_TIMEOUT_S = 30.0
_GENERATE_TIMEOUT_S = 120.0


class FactoryClientError(Exception):
    """A non-2xx (or transport) failure talking to the VKPower factory.

    ``status_code`` is the upstream HTTP status (0 for a transport error) so the
    router can propagate it faithfully; ``detail`` is the upstream honest reason.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"factory call failed ({status_code}): {detail}")


def _rtm_path(artifact_id: str) -> str:
    return f"/api/v1/test-factory/{artifact_id}/rtm"


def _generate_path(artifact_id: str) -> str:
    return f"/api/v1/test-factory/{artifact_id}/generate"


async def _call(
    *, method: str, path: str, endpoint: str, tenant_id: str, timeout_s: float,
    json_body: dict | None = None,
) -> dict:
    """Issue one authenticated factory call; raise :class:`FactoryClientError`
    on transport failure or any non-200 response.

    Phase-5.5: PROPAGATES the correlation id (``X-Request-ID``) to VKPower so one
    id spans QE-Central and the factory, and RECORDS the factory-latency metric
    on EVERY outcome under the LOGICAL ``endpoint`` label (never the
    artifact-scoped ``path`` — keeps the metric's label cardinality bounded).
    ``status`` recorded is the upstream HTTP code, or ``transport_error`` when the
    request never got a response."""
    token = mint_service_jwt(tenant_id)
    headers = outbound_request_id_headers({"Authorization": f"Bearer {token}"})
    started = time.monotonic()
    status: object = "transport_error"
    try:
        try:
            async with httpx.AsyncClient(
                base_url=settings.platform_api_url, timeout=timeout_s,
            ) as client:
                response = await client.request(
                    method, path, headers=headers,
                    json=json_body if json_body is not None else None,
                )
        except Exception as exc:  # transport failure — honest, typed
            logger.warning(
                "qec.factory.transport_error",
                extra={"tenant_id": tenant_id, "path": path, "error": str(exc)[:300]},
            )
            raise FactoryClientError(0, f"transport error: {exc}"[:300]) from exc

        status = response.status_code
        if response.status_code == 200:
            try:
                body = response.json()
            except Exception as exc:
                raise FactoryClientError(502, "factory returned a non-JSON 200 body") from exc
            return body if isinstance(body, dict) else {"result": body}

        detail = ""
        try:
            detail = str(response.json().get("detail") or "")
        except Exception:
            detail = response.text[:300]
        logger.warning(
            "qec.factory.rejected",
            extra={"tenant_id": tenant_id, "path": path,
                   "status_code": response.status_code, "detail": detail[:300]},
        )
        raise FactoryClientError(response.status_code, detail[:300] or "factory call failed")
    finally:
        record_factory_call(
            endpoint=endpoint, method=method, status=status,
            duration_seconds=time.monotonic() - started,
        )


async def get_rtm(*, tenant_id: str, artifact_id: str) -> dict:
    """Fetch the ``/rtm`` traceability matrix for an artifact (service JWT).

    Returns ``{artifact_id, tests: [...]}``.  A 404 (unknown/foreign artifact)
    propagates as :class:`FactoryClientError` with status 404.

    An idempotent read: a TRANSIENT failure (transport error / 502 / 503 / 504)
    is retried with bounded backoff (``QEC_HTTP_MAX_RETRIES``, default 2); a
    DETERMINISTIC status (404 / 403 / 422 / …) is raised on the first attempt,
    unretried — retrying it would only amplify a real error.
    """
    return await call_with_retries(
        lambda: _call(
            method="GET", path=_rtm_path(artifact_id), endpoint="rtm",
            tenant_id=tenant_id, timeout_s=_RTM_TIMEOUT_S,
        ),
        idempotent=True, op="rtm",
    )


async def generate(*, tenant_id: str, artifact_id: str, answer_key: dict | None = None) -> dict:
    """Trigger ``POST …/generate`` for an artifact (service JWT).

    Returns the factory's generate summary (``{success, generated, demonstrated,
    …}``).  A 404 / 403 / 503 propagates as :class:`FactoryClientError`.

    NON-idempotent (it materialises grounded cases and could double-submit), so
    it is executed EXACTLY ONCE — never auto-retried — while its latency and the
    correlation id are still recorded / propagated by :func:`_call`.

    ``answer_key`` (ANSWERS P1): the client's rich key.  When non-empty it is sent
    as the JSON body so the factory can seed form fills AND build the value oracle
    (``outcomes``/``rules``).  When empty/None the POST stays BODY-LESS — byte-for-
    byte the historical call — so every existing caller is unaffected.
    """
    body = dict(answer_key) if answer_key else None
    return await _call(
        method="POST", path=_generate_path(artifact_id), endpoint="generate",
        tenant_id=tenant_id, timeout_s=_GENERATE_TIMEOUT_S,
        json_body={"answer_key": body} if body else None,
    )


# ─── Runnable Journeys (Release D) — case list + run dispatch + status ───────

_CASES_TIMEOUT_S = 30.0
_RUN_DISPATCH_TIMEOUT_S = 60.0
_RUN_STATUS_TIMEOUT_S = 30.0


async def list_test_cases(
    *, tenant_id: str, artifact_id: str, limit: int = 200,
) -> list[dict]:
    """Every ACTIVE test case of an artifact (service JWT), paginated to
    exhaustion. Each item carries ``test_case_id``, ``name``, ``type``,
    ``step_count`` and the full ``test_case`` JSON (whose ``steps[].observed``
    is what the journey linker matches on). Idempotent read — retried."""
    items: list[dict] = []
    page = 1
    while True:
        body = await call_with_retries(
            lambda p=page: _call(
                method="GET",
                path=(f"/api/v1/test-factory/{artifact_id}/test-cases"
                      f"?page={p}&limit={min(limit, 200)}&status=active"),
                endpoint="test_cases", tenant_id=tenant_id,
                timeout_s=_CASES_TIMEOUT_S,
            ),
            idempotent=True, op="test_cases",
        )
        batch = body.get("items") or []
        items.extend(b for b in batch if isinstance(b, dict))
        total = int(body.get("total") or 0)
        if not batch or len(items) >= min(total, limit):
            return items[:limit]
        page += 1


async def run_cases(
    *, tenant_id: str, artifact_id: str, test_ids: list[str],
    environment_id: str = "", journey_payloads: list[dict] | None = None,
) -> dict:
    """Dispatch a real runner execution of ``test_ids`` (service JWT).

    Returns the factory's dispatch body — ``{run_id, status: 'running', ...}``
    on success, or a ``blocked`` body (member-data gate). NON-idempotent —
    executed exactly once, never auto-retried; a 409 (quarantined /
    uncertified) or 404 (no matching active cases) propagates as
    :class:`FactoryClientError` for the caller to record honestly."""
    body: dict = {"test_ids": [str(t) for t in test_ids if str(t).strip()]}
    if journey_payloads:
        body["journey_payloads"] = list(journey_payloads)
    if environment_id.strip():
        body["environment_id"] = environment_id.strip()
    return await _call(
        method="POST",
        path=f"/api/v1/test-factory/{artifact_id}/playwright/run",
        endpoint="playwright_run", tenant_id=tenant_id,
        timeout_s=_RUN_DISPATCH_TIMEOUT_S, json_body=body,
    )


async def run_cases_live(
    *, tenant_id: str, artifact_id: str, test_ids: list[str],
    environment_id: str = "",
) -> dict:
    """Dispatch a HEADED, WATCHABLE execution of ``test_ids`` (service JWT).

    Same runner, same evidence, same heal/attribution as :func:`run_cases` —
    it additionally returns ``live_url``, the noVNC viewer address for the
    session, so an operator can watch the journey execute. The session is
    transient: the runner tears it down shortly after the run ends."""
    body: dict = {"test_ids": [str(t) for t in test_ids if str(t).strip()]}
    if environment_id.strip():
        body["environment_id"] = environment_id.strip()
    return await _call(
        method="POST",
        path=f"/api/v1/test-factory/{artifact_id}/playwright/run-live",
        endpoint="playwright_run_live", tenant_id=tenant_id,
        timeout_s=_RUN_DISPATCH_TIMEOUT_S, json_body=body,
    )


async def run_status(
    *, tenant_id: str, artifact_id: str, run_id: str,
) -> dict:
    """The transient runner-job status for a dispatch ``run_id``.

    ``status``: running → passed | failed | timed_out | error (plus blocked /
    idle / unknown). Idempotent read — retried."""
    return await call_with_retries(
        lambda: _call(
            method="GET",
            path=f"/api/v1/test-factory/{artifact_id}/playwright/run/{run_id}",
            endpoint="playwright_run_status", tenant_id=tenant_id,
            timeout_s=_RUN_STATUS_TIMEOUT_S,
        ),
        idempotent=True, op="playwright_run_status",
    )


async def list_runs(
    *, tenant_id: str, artifact_id: str, limit: int = 10,
) -> list[dict]:
    """Recent DURABLE ingested runs (the evidence/report keys).

    Each entry's ``run_header`` carries ``run_id`` (the ingested id),
    ``ci_run_id`` (== the dispatch run_id — the correlation key), ``status``
    and step totals. Idempotent read — retried."""
    body = await call_with_retries(
        lambda: _call(
            method="GET",
            path=f"/api/v1/test-factory/{artifact_id}/runs?limit={int(limit)}",
            endpoint="runs_list", tenant_id=tenant_id,
            timeout_s=_RUN_STATUS_TIMEOUT_S,
        ),
        idempotent=True, op="runs_list",
    )
    runs = body.get("runs") if isinstance(body.get("runs"), list) else (
        body.get("result") if isinstance(body.get("result"), list) else [])
    return [r for r in runs if isinstance(r, dict)]


async def compile_journeys(
    *, tenant_id: str, journeys: list[dict], parametrize: bool = False,
) -> dict:
    """M2.4 / T-GEN-01 — compile RANKED journey payloads into Playwright specs.

    The payloads are what ``journey_spec.build_journey_case`` produced: a
    journey's own walked evidence, sufficient on its own, with no dependence on
    an adopted artifact-level case.  The factory compiles each through the SAME
    ``compile_case`` every other spec goes through, LINTS the result and returns
    the lint verdict alongside it (T-GEN-05).

    Deliberately NOT retried.  Compilation is pure and deterministic, so a
    transient blip has nothing to re-derive differently — and the endpoint is a
    POST, which the retry helper is right to refuse on principle.  A failure
    propagates as :class:`FactoryClientError` with the upstream reason.
    """
    return await _call(
        method="POST",
        path="/api/v1/test-factory/journeys/compile",
        endpoint="journeys_compile", tenant_id=tenant_id,
        timeout_s=_GENERATE_TIMEOUT_S,
        json_body={"journeys": list(journeys or []),
                   "parametrize": bool(parametrize)},
    )


__all__ = ["FactoryClientError", "get_rtm", "generate", "compile_journeys"]
