"""QE-Central — the ONE fleet-wide tenant enumeration source (M3.3 / T-FL-05).

WHY THIS MODULE EXISTS
======================
Every table the control plane scans (``client_apps``, ``app_cycles``,
``change_events``, ``qe_explorations``) carries ``ENABLE`` **and** ``FORCE ROW
LEVEL SECURITY`` with a ``tenant_isolation`` policy keyed on the
``nexus.current_tenant_id`` GUC.  Under the production posture — the ``qec``
role is ``NOSUPERUSER`` and ``NOBYPASSRLS`` — a fleet-wide query that sets NO
GUC evaluates ``tenant_id = current_setting('nexus.current_tenant_id', true)``
against ``NULL``, which is never true.  It therefore returns **zero rows**, and
a scheduler built on it discovers no work at all while reporting no error.

Measured on a production-like database (``qec`` = ``rolsuper f`` /
``rolbypassrls f``, FORCE RLS on all four tables)::

    no GUC set (what _scan_fleet did)      →  0 apps visible
    GUC = tenant_a                         →  1 app visible
    tenant_a sees app_a1 / tenant_b sees app_b1 (isolation holds)

THE FIX IS NOT TO WEAKEN RLS.  It is to enumerate tenants ONCE and then run
every data read inside a transaction scoped to ONE tenant's GUC, exactly as the
stale-crawl reaper already does.  Tenant isolation is therefore *preserved*
during discovery: a scan for tenant A physically cannot return tenant B's rows,
because the policy is still enforcing on every statement.

WHY IT IS SHARED
================
This enumeration previously lived as a private ``_tenant_ids`` inside
``controlplane/reaper.py``.  The cycle driver's ``_scan_fleet`` never adopted
it and stayed GUC-less — two copies of one policy decision, one of them wrong.
Both now call this function, so the rule cannot drift again.

THE REGISTRY
============
The ``tenants`` registry is a GLOBAL, non-tenant-scoped table in the *substrate*
(nexus) database — it is the one place that legitimately lists every tenant, and
it holds no tenant business data, only identity.  Reading it is not a privilege
escalation: it yields the set of tenant IDs, never one tenant's rows.

FAIL-SOFT, NEVER FAIL-SILENT.  If the registry cannot be read the caller falls
back to the platform scope (``__platform__``) so the daemon degrades to
single-scope work rather than silently doing nothing — and the failure is logged
at WARNING with the cause, never swallowed.
"""
from __future__ import annotations

import logging

from sqlalchemy import text

from ..db import substrate_engine

logger = logging.getLogger(__name__)

#: The scope used when the global registry cannot be read. Matches the reaper's
#: long-standing fallback so both daemons degrade identically.
PLATFORM_SCOPE = "__platform__"


async def fleet_tenant_ids() -> list[str]:
    """Every tenant id in the fleet, for a per-tenant RLS-scoped scan.

    Returns at least ``[PLATFORM_SCOPE]`` — never an empty list, because an
    empty list is indistinguishable from "the fleet is idle" at the call site
    and would reintroduce the silent no-op this module exists to kill.
    """
    try:
        async with substrate_engine.begin() as conn:
            rows = (await conn.execute(text("SELECT tenant_id FROM tenants"))).all()
        ids = [str(r[0]) for r in rows if r[0]]
        return ids or [PLATFORM_SCOPE]
    except Exception as exc:  # registry unreadable → platform scope, loudly
        logger.warning(
            "qec.controlplane.tenant_enum_failed",
            extra={"error": str(exc)[:200]},
        )
        return [PLATFORM_SCOPE]


async def scope_to_tenant(conn, tenant_id: str) -> None:
    """Scope an OPEN transaction to ``tenant_id`` so RLS admits exactly its rows.

    ``set_config(..., true)`` is transaction-local, so the scope cannot leak into
    the next tenant's iteration on a pooled connection — the property that makes
    a per-tenant loop safe to run on a shared pool.
    """
    await conn.execute(
        text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
        {"t": str(tenant_id)},
    )
