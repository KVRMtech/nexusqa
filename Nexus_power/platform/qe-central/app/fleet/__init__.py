"""QE-Central — fleet-scale operations package (Phase-7).

Additive, OPT-IN capabilities that let ONE control plane serve a FLEET of
clients (20+ tenants / 10k+ apps) fairly and economically WITHOUT changing any
existing behaviour:

  * :mod:`.quota` — per-tenant quota + plan-tiering.  A :class:`~app.fleet.quota.QuotaPlan`
    caps a tenant's apps / concurrent cycles / monthly metered spend / default
    politeness rate / retention window; a resolver attaches a plan to a tenant
    from config with a GENEROUS default (all-unlimited) so today's tenants are
    untouched.  Enforcement is FAIL-CLOSED and OPT-IN: a limit bites only when a
    plan explicitly sets it, and the default plan never trips — so the whole
    existing test suite stays green.
  * :mod:`.lifecycle` — the pure tenant lifecycle state machine + the fail-closed
    :func:`assert_tenant_operational` gate (a suspended tenant's crawls/cycles are
    refused).
  * :mod:`.rbac` — the PLATFORM SUPER-ADMIN scope + principal minting (a tenant
    admin can NOT provision other tenants).
  * :mod:`.provisioning` — onboard/suspend/resume/offboard a CLIENT in one call
    (:func:`provision_tenant` and friends).

Nothing in this package runs until a caller imports it; the defaults preserve
the single-tenant, unlimited behaviour byte-for-byte.  A tenant with no
``tenant_provisioning`` control record behaves exactly as today (active).
"""
from __future__ import annotations

from .lifecycle import (
    STATUS_ACTIVE,
    STATUS_DELETED,
    STATUS_OFFBOARDING,
    STATUS_SUSPENDED,
    TenantLifecycleError,
    TenantNotOperational,
    TenantProvisioningRecord,
    assert_tenant_operational,
    is_operational,
    resolve_transition,
)
from .provisioning import (
    LifecycleResult,
    ProvisioningError,
    TenantHandle,
    assert_tenant_operational_db,
    ensure_deploy_safe,
    get_tenant_provisioning,
    load_tenant_provisioning,
    offboard_tenant,
    provision_tenant,
    resume_tenant,
    suspend_tenant,
)
from .rbac import (
    PLATFORM_ADMIN_CLAIM,
    is_platform_admin,
    mint_platform_admin_jwt,
    mint_tenant_principal_jwt,
    require_platform_admin,
)
from .quota import (
    BUILTIN_PLANS,
    DEFAULT_PLAN,
    DEFAULT_PLAN_NAME,
    ENV_QUOTA_PLANS,
    ENV_TENANT_PLANS,
    RESOURCE_APPS,
    RESOURCE_CONCURRENT_CYCLES,
    RESOURCE_MONTHLY_BROWSER_SECONDS,
    RESOURCE_MONTHLY_LLM_TOKENS,
    QuotaDecision,
    QuotaExceeded,
    QuotaPlan,
    check_quota,
    effective_max_rps,
    enforce_app_registration_quota,
    enforce_cycle_quota,
    load_plan_registry,
    load_tenant_assignments,
    month_start,
    resolve_plan,
    retention_cutoff,
    sum_unit,
)

__all__ = [
    "BUILTIN_PLANS",
    "DEFAULT_PLAN",
    "DEFAULT_PLAN_NAME",
    "ENV_QUOTA_PLANS",
    "ENV_TENANT_PLANS",
    "RESOURCE_APPS",
    "RESOURCE_CONCURRENT_CYCLES",
    "RESOURCE_MONTHLY_BROWSER_SECONDS",
    "RESOURCE_MONTHLY_LLM_TOKENS",
    "QuotaDecision",
    "QuotaExceeded",
    "QuotaPlan",
    "check_quota",
    "effective_max_rps",
    "enforce_app_registration_quota",
    "enforce_cycle_quota",
    "load_plan_registry",
    "load_tenant_assignments",
    "month_start",
    "resolve_plan",
    "retention_cutoff",
    "sum_unit",
    # ── Phase-7 lifecycle ──
    "STATUS_ACTIVE", "STATUS_SUSPENDED", "STATUS_OFFBOARDING", "STATUS_DELETED",
    "TenantLifecycleError", "TenantNotOperational", "TenantProvisioningRecord",
    "assert_tenant_operational", "is_operational", "resolve_transition",
    # ── Phase-7 rbac ──
    "PLATFORM_ADMIN_CLAIM", "is_platform_admin", "mint_platform_admin_jwt",
    "mint_tenant_principal_jwt", "require_platform_admin",
    # ── Phase-7 provisioning ──
    "LifecycleResult", "ProvisioningError", "TenantHandle",
    "assert_tenant_operational_db", "ensure_deploy_safe", "get_tenant_provisioning",
    "load_tenant_provisioning", "offboard_tenant", "provision_tenant",
    "resume_tenant", "suspend_tenant",
]
