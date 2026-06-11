"""
Platform API — Admin routes (Module 12).

Engine admin, resource monitoring, integrations, audit log, users.
All endpoints require JWT authentication and enforce tenant isolation.
"""
from __future__ import annotations

import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, desc

from nexus_sdk.db.models import AuditLogRow, UserRow

from ..auth import get_current_user
from ..database import require_db, utc_now, tenant_scoped_session
from ..services.flywheel import export as flywheel_export

router = APIRouter(tags=["Admin"])

# ─── Module-level state (set at app init) ─────────────────────
_http: httpx.AsyncClient | None = None
_cache = None
_config = None


def init_admin(http_client: httpx.AsyncClient, cache, config) -> None:
    """Initialise module-level singletons used by admin endpoints."""
    global _http, _cache, _config
    _http = http_client
    _cache = cache
    _config = config


# ─── Engine List ──────────────────────────────────────────────

# Canonical pipeline requires exactly these 5 engines.
_CANONICAL_ENGINE_NAMES = frozenset({"Ears", "Eyes", "Shield", "Spine", "Brain"})


@router.get("/api/v1/admin/engines")
async def get_admin_engines(user: dict = Depends(get_current_user)):
    async def _fetch():
        engine_defs = [
            ("Ears", "Speech-to-Text", _config.ears_engine_url),
            ("Eyes", "Visual Analysis", _config.eyes_engine_url),
            ("Heart", "LLM / AI Core", _config.heart_engine_url),
            ("Backbone", "Knowledge Graph", _config.backbone_engine_url),
            ("Shield", "PII Protection", _config.shield_engine_url),
            ("Nerves", "Integrations", _config.nerves_engine_url),
            ("Hands", "Test Data Generator", _config.hands_engine_url),
            ("Legs", "Test Executor", _config.legs_engine_url),
            ("Spine", "Document Ingestion", _config.spine_engine_url),
            ("Mouth", "Report Generator", _config.mouth_engine_url),
            ("Brain", "Intelligent Coordinator", _config.brain_engine_url),
        ]
        engines = []
        all_signoff_ready = True
        for name, code_name, url in engine_defs:
            checked_at = datetime.now(timezone.utc).isoformat()
            entry = {
                "name": name, "codeName": code_name,
                "status": "unreachable", "mode": "unknown",
                "modes": {},
                "uptime": "unknown", "version": "0.1.0",
                "cpu": 0, "memory": 0, "requests24h": 0, "errors24h": 0,
                "last_checked_at": checked_at,
                "health_error": None,
                "signoff_ready": False,
            }
            try:
                resp = await _http.get(f"{url}/health/detail", timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    raw_status = data.get("status", "unknown")
                    if raw_status == "healthy":
                        entry["status"] = "healthy"
                    elif raw_status in ("degraded", "warming"):
                        entry["status"] = "degraded"
                    else:
                        entry["status"] = "degraded"
                    modes = data.get("modes", {})
                    entry["modes"] = modes
                    # Compute aggregate mode: "real" if all non-stub, else
                    # "mixed" if some real + some stub, else "stub"
                    stub_count = sum(1 for v in modes.values() if v == "stub" or "stub" in str(v).lower())
                    real_count = len(modes) - stub_count
                    if stub_count == 0 and modes:
                        entry["mode"] = "real"
                    elif real_count > 0 and stub_count > 0:
                        entry["mode"] = "mixed"
                    elif stub_count > 0 and real_count == 0:
                        entry["mode"] = "stub"
                    else:
                        entry["mode"] = "real" if entry["status"] == "healthy" else "unknown"
                    entry["uptime"] = data.get("uptime", "unknown")
                    entry["version"] = data.get("version", "0.1.0")
                    # Signoff readiness: healthy or degraded WITH no stubs
                    entry["signoff_ready"] = (
                        entry["status"] in ("healthy",)
                        and entry["mode"] in ("real",)
                    )
                else:
                    entry["status"] = "degraded"
                    entry["health_error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:
                entry["health_error"] = f"{type(exc).__name__}: {exc}"
            if not entry["signoff_ready"]:
                all_signoff_ready = False
            engines.append(entry)

        # ── Canonical-only readiness ──────────────────────────
        canonical_engines = [
            e for e in engines if e["name"] in _CANONICAL_ENGINE_NAMES
        ]
        canonical_blockers = []
        for e in canonical_engines:
            if e["status"] != "healthy":
                canonical_blockers.append(f"{e['name']}: status={e['status']}")
            elif e["mode"] not in ("real",):
                canonical_blockers.append(f"{e['name']}: mode={e['mode']}")
        canonical_signoff_ready = len(canonical_blockers) == 0 and len(canonical_engines) == len(_CANONICAL_ENGINE_NAMES)

        # Control plane health: gateway, orchestrator, auth
        control_plane_healthy = True
        control_plane_status = []
        control_plane_defs = [
            ("gateway", _config.gateway_url, "/health"),
            ("orchestrator", _config.orchestrator_url, "/health"),
            ("auth", _config.auth_service_url, "/health"),
        ]
        for cp_name, cp_url, cp_path in control_plane_defs:
            cp_entry = {"name": cp_name, "status": "unreachable", "error": None}
            try:
                cp_resp = await _http.get(f"{cp_url}{cp_path}", timeout=3.0)
                if cp_resp.status_code == 200:
                    cp_entry["status"] = "healthy"
                else:
                    cp_entry["status"] = "degraded"
                    cp_entry["error"] = f"HTTP {cp_resp.status_code}"
                    control_plane_healthy = False
            except Exception as exc:
                cp_entry["error"] = f"{type(exc).__name__}: {exc}"
                control_plane_healthy = False
            control_plane_status.append(cp_entry)

        if not control_plane_healthy:
            for cp in control_plane_status:
                if cp["status"] != "healthy":
                    canonical_blockers.append(f"{cp['name']}: {cp['status']}")

        canonical_operator_ready = canonical_signoff_ready and control_plane_healthy

        return {
            "engines": engines,
            "signoff_ready": all_signoff_ready,
            "canonical_signoff_ready": canonical_signoff_ready,
            "canonical_engines": canonical_engines,
            "canonical_blockers": canonical_blockers,
            "canonical_operator_ready": canonical_operator_ready,
            "control_plane": control_plane_status,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    return await _cache.get_or_set("admin_engines", _fetch, ttl=15)


# ─── System Resources ────────────────────────────────────────

@router.get("/api/v1/admin/resources")
async def get_admin_resources(user: dict = Depends(get_current_user)):
    async def _fetch():
        try:
            import psutil as _ps  # type: ignore[import-not-found]
            cpu_percent = _ps.cpu_percent(interval=0.1)
            mem = _ps.virtual_memory()
            disk = _ps.disk_usage("/")
            return {
                "gpu": {"label": "GPU (CUDA)", "used": 0, "total": 0, "unit": "GB"},
                "ram": {"label": "System RAM", "used": round(mem.used / 1e9, 1), "total": round(mem.total / 1e9, 1), "unit": "GB"},
                "cpu": {"label": "CPU Cores", "used": round(cpu_percent), "total": 100, "unit": "%"},
                "storage": {"label": "Storage", "used": round(disk.used / 1e9, 1), "total": round(disk.total / 1e9, 1), "unit": "GB"},
            }
        except ImportError:
            return {
                "gpu": {"label": "GPU (CUDA)", "used": 0, "total": 0, "unit": "GB"},
                "ram": {"label": "System RAM", "used": 0, "total": 0, "unit": "GB"},
                "cpu": {"label": "CPU Cores", "used": 0, "total": 100, "unit": "%"},
                "storage": {"label": "Storage", "used": 0, "total": 0, "unit": "GB"},
            }
    return await _cache.get_or_set("admin_resources", _fetch, ttl=10)


# ─── Integrations ────────────────────────────────────────────

@router.get("/api/v1/admin/integrations")
async def get_admin_integrations(
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    integrations = []
    try:
        resp = await _http.get(f"{_config.nerves_engine_url}/api/v1/nerves/connectors", timeout=5.0)
        if resp.status_code == 200:
            connectors = resp.json()
            for c in connectors.get("connectors", connectors) if isinstance(connectors, dict) else connectors:
                name = c.get("name", c) if isinstance(c, dict) else c
                integrations.append({
                    "name": name,
                    "type": c.get("type", "Connector") if isinstance(c, dict) else "Connector",
                    "status": "connected" if c.get("configured") else "disconnected" if isinstance(c, dict) else "disconnected",
                    "lastSync": c.get("last_sync", "Never") if isinstance(c, dict) else "Never",
                })
    except Exception:
        pass
    if not integrations:
        integrations = [
            {"name": "Jira Cloud", "type": "Issue Tracker", "status": "disconnected", "lastSync": "Never"},
            {"name": "GitHub", "type": "Source Control", "status": "disconnected", "lastSync": "Never"},
            {"name": "Slack", "type": "Messaging", "status": "disconnected", "lastSync": "Never"},
        ]
    return integrations


# ─── Audit Log ────────────────────────────────────────────────

@router.get("/api/v1/admin/audit")
async def get_audit_log(
    user: dict = Depends(get_current_user),
):
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(AuditLogRow)
            .where(AuditLogRow.tenant_id == tenant_id)
            .order_by(desc(AuditLogRow.created_at))
            .limit(100)
        )
        return [
            {
                "id": r.log_id,
                "timestamp": r.created_at.isoformat() if r.created_at else "",
                "user": r.user_id,
                "action": r.action,
                "resource": f"{r.entity_type}/{r.entity_id}" if r.entity_type else r.entity_id,
                "details": r.details if isinstance(r.details, str) else str(r.details.get("resolution", "")) if isinstance(r.details, dict) else "",
            }
            for r in result.scalars().all()
        ]


# ─── Users ────────────────────────────────────────────────────

@router.get("/api/v1/admin/users")
async def get_admin_users(
    user: dict = Depends(get_current_user),
):
    """Real users from PostgreSQL users table."""
    tenant_id = user["tenant_id"]
    factory = require_db()
    async with factory() as db:
        result = await db.execute(
            select(UserRow)
            .where(UserRow.tenant_id == tenant_id)
            .order_by(desc(UserRow.last_login))
        )
        rows = result.scalars().all()
        if not rows:
            return []
        now = utc_now()
        users = []
        for r in rows:
            if r.last_login:
                delta = now - r.last_login
                secs = delta.total_seconds()
                if secs < 300:
                    last_active = "Just now"
                elif secs < 3600:
                    last_active = f"{int(secs / 60)} min ago"
                elif secs < 86400:
                    last_active = f"{int(secs / 3600)} hr ago"
                else:
                    last_active = f"{delta.days} day{'s' if delta.days > 1 else ''} ago"
            else:
                last_active = "Never"
            users.append({
                "name": r.name, "email": r.email, "role": r.role or "Viewer",
                "lastActive": last_active, "status": "active" if r.is_active else "inactive",
            })
        return users


# ─── Flywheel consented-export channel (Phase 2) ──────────────

@router.get("/api/v1/admin/flywheel/export-preview")
async def flywheel_export_preview(
    vertical: str = Query("", max_length=40),
    user: dict = Depends(get_current_user),
):
    """Admin read surface for the consented-export channel — THIS tenant's
    de-identified, k-anonymized, (stub-)DP aggregate (counts only, NEVER raw rows;
    the invariant ``raw_rows_exported == 0``). It surfaces *exactly what would leave
    the building* so an admin can audit it before any federation. OFF (returns
    ``enabled: false``) unless ``NEXUS_FLYWHEEL_EXPORT`` is set — capturing a label
    never means it may be exported. Tenant-scoped via RLS; the cross-tenant
    federation loop + real DP/secure-aggregation remain deferred to the privacy
    build (see export.add_dp_noise stub)."""
    tenant_id = user["tenant_id"]
    if not flywheel_export.export_enabled():
        return {
            "enabled": False, "groups": [], "group_count": 0, "raw_rows_exported": 0,
            "note": "Consented export is OFF. Set NEXUS_FLYWHEEL_EXPORT=1 to enable.",
        }
    async with tenant_scoped_session(tenant_id) as session:
        return await flywheel_export.build_tenant_export(
            session, tenant_id=tenant_id, vertical=vertical or "",
        )
