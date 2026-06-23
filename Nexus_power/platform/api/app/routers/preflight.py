"""Live Preflight endpoint — ADDITIVE (does not modify the frozen test-factory
router or compiler). Runs the deterministic locator-resolution probe via the
nexus-runner and returns a per-step report: does each compiled locator resolve to
exactly one live element (0 = broken/renamed, >1 = ambiguous), do select options
exist, are URLs reachable. No LLM — Playwright API used as a deterministic oracle.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi import Path as PathParam

from ..auth import get_current_user
from ..database import tenant_scoped_session
from ..services.test_factory import service as factory_service
from ..services.test_factory import auth_profiles
from ..services.script_factory import build_field_meta, runner_client, preflight

router = APIRouter()


@router.post("/api/v1/test-factory/{artifact_id}/scripts/{test_id}/preflight")
async def script_preflight(
    request: Request,
    artifact_id: str = PathParam(..., min_length=1, max_length=64),
    test_id: str = PathParam(..., min_length=1, max_length=64),
    body: dict = Body(default={}),
    user: dict = Depends(get_current_user),
):
    """Open the live app and check, per step, that the compiled locator resolves
    (1 = good, 0 = broken/renamed, >1 = ambiguous) + that <select> options exist.
    Deterministic Playwright probe via the runner; no LLM. Needs a reachable
    base_url (recorded URLs are host-less paths). Reuses the artifact's captured
    login session (if any) so the probe sees authenticated pages, not a login wall."""
    tenant_id = user["tenant_id"]
    envelope = getattr(request.app.state, "envelope_service", None)
    storage_state = None
    async with tenant_scoped_session(tenant_id) as session:
        cases = await factory_service.load_active_production_cases(
            session, artifact_id=artifact_id)
        visits, _ = await factory_service._load_current_pages_and_actions(
            session, artifact_id=artifact_id)
        # Decrypt the captured auth session for this artifact (same source a real run
        # uses). Auth is optional: any failure -> unauthenticated probe, as before.
        try:
            storage_state = await auth_profiles.get_storage_state(
                session, envelope=envelope, tenant_id=tenant_id, artifact_id=artifact_id)
        except Exception:
            storage_state = None
    field_meta = build_field_meta(visits)
    tc = next((c for c in cases if (getattr(c, "test_id", "") or "") == test_id), None)
    if tc is None:
        raise HTTPException(status_code=404, detail="no active test case for this script")

    base_url = (str(body.get("base_url") or "")).strip()
    files = preflight.build_preflight_files(
        tc, field_meta, base_url=base_url, storage_state=storage_state)
    env = {"NEXUS_BASE_URL": base_url} if base_url else {}
    try:
        result = await runner_client.run_suite(files, env, timeout_ms=120000)
    except Exception as e:  # transport / runner error
        raise HTTPException(status_code=502, detail=f"preflight runner error: {e}")

    report = preflight.parse_preflight_output(result.get("output", "") or "")
    report["runner_status"] = result.get("status")
    report["base_url"] = base_url
    report["authenticated"] = bool(storage_state)
    if not base_url:
        report["note"] = ("no base_url supplied — recorded URLs are host-less paths, "
                          "so locators cannot resolve; set the Environment URL and retry.")
    return report

# NOTE: the deployed build of this router ALSO exposes a GET .../heal-events read
# endpoint (Part-11 heal evidence chain). It is intentionally omitted here because it
# depends on the heal-evidence subsystem (heal_evidence.py + SDK HealEventRow + migration
# 038) which has not yet landed in this repo. It re-lands together with that batch.
