"""QE-Central — fail-closed boot-safety validation (Phase-6 safety spine).

Grounded in an adversarial finding: today the service fails OPEN at boot — a
development KEK is silently used in production, the JWT/HMAC secrets keep their
known dev defaults, and nothing refuses to start before a real client's app or
credentials touch the system.

:func:`validate_boot_safety` closes that hole.  It enumerates every
security-critical setting that is still wearing a development default and, in a
DEPLOYED environment (``NEXUS_ENV`` in ``{staging, production}``), REFUSES TO
BOOT by raising :class:`BootSafetyError` (the caller — ``main.lifespan`` — lets
it propagate so the process exits, fail-closed).  In ``development``/``test``
(the default ``NEXUS_ENV``) it only WARNS, so local dev and the full existing
test suite boot unchanged.

Design notes:
  * PURE + unit-testable — pass any settings-like object exposing the read
    attributes (``nexus_env``, ``nexus_kek_provider``, ``nexus_jwt_secret``,
    ``qec_explorer_token``, ``qec_database_url``, ``nexus_database_url_substrate``).
  * NEVER leaks a secret value: violation messages name the OFFENDING SETTING
    and WHY, never the parsed password/secret.
  * Additive: the dev-default sentinels live in :mod:`app.config` so there is a
    single source of truth shared with the rest of the safety spine.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from ..config import (
    DEPLOYED_ENVS,
    DEV_DEFAULT_DB_PASSWORDS,
    DEV_DEFAULT_EXPLORER_TOKENS,
    DEV_DEFAULT_JWT_SECRETS,
    DEV_KEK_PROVIDER,
)

logger = logging.getLogger(__name__)


class BootSafetyError(RuntimeError):
    """Raised to REFUSE booting a deployed process wearing dev defaults.

    Carries the full :attr:`violations` list so the operator sees every unsafe
    setting at once (fail-closed, but with an actionable diagnosis).
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        detail = "; ".join(self.violations) or "unknown"
        super().__init__(
            f"QE-Central refuses to boot - {len(self.violations)} unsafe "
            f"production setting(s): {detail}"
        )


def _is_deployed(nexus_env: str) -> bool:
    """True when ``nexus_env`` is a deployed env (``staging``/``production``).

    Any other value — including ``development``, ``test``, an empty string, or
    an unknown label — is treated as NON-deployed (WARN-only), so the gate is
    inert everywhere except a genuinely deployed process.
    """
    return (nexus_env or "").strip().lower() in DEPLOYED_ENVS


def _dsn_has_default_password(dsn: str) -> bool:
    """True when ``dsn`` carries an empty or known-dev DB password.

    Parses the password out of the DSN's authority and tests membership in
    :data:`app.config.DEV_DEFAULT_DB_PASSWORDS`.  The password value itself is
    NEVER returned or logged — only the boolean verdict.  A DSN we cannot parse
    (or one with no userinfo at all) is treated as NOT-a-default here; other
    checks still cover the security-critical secrets.
    """
    try:
        password = urlsplit((dsn or "").strip()).password
    except Exception:  # malformed DSN — do not crash the boot check on it
        return False
    if password is None:
        return False
    return password.strip().lower() in DEV_DEFAULT_DB_PASSWORDS


def collect_boot_violations(settings) -> list[str]:
    """Enumerate security-critical settings still at a development default.

    PURE and env-AGNOSTIC: it does NOT consult ``NEXUS_ENV`` — it just reports
    what is unsafe.  :func:`validate_boot_safety` decides whether that is fatal
    (deployed) or a warning (dev/test).  Messages never contain a secret value.
    """
    violations: list[str] = []

    kek = (getattr(settings, "nexus_kek_provider", "") or "").strip().lower()
    if kek == DEV_KEK_PROVIDER:
        violations.append(
            "NEXUS_KEK_PROVIDER=local - the development KEK (no KMS-backed "
            "envelope encryption); set gcp_kms or aws_kms before storing "
            "client credentials"
        )

    jwt_secret = (getattr(settings, "nexus_jwt_secret", "") or "").strip()
    if jwt_secret in DEV_DEFAULT_JWT_SECRETS:
        violations.append(
            "NEXUS_JWT_SECRET is empty or a known development default - set a "
            "strong random secret"
        )

    explorer_token = (getattr(settings, "qec_explorer_token", "") or "").strip()
    if explorer_token in DEV_DEFAULT_EXPLORER_TOKENS:
        violations.append(
            "QEC_EXPLORER_TOKEN is empty or the development default - the "
            "explorer seam would be unauthenticated"
        )

    for label, attr in (
        ("QEC_DATABASE_URL", "qec_database_url"),
        ("NEXUS_DATABASE_URL_SUBSTRATE", "nexus_database_url_substrate"),
    ):
        dsn = getattr(settings, attr, "") or ""
        if _dsn_has_default_password(dsn):
            violations.append(
                f"{label} carries an empty or default development DB password"
            )

    return violations


#: KEK providers that are backed by a real KMS (production-grade envelope
#: encryption).  Anything else (notably ``local``) is a development KEK.
PRODUCTION_GRADE_KEK_PROVIDERS = frozenset({"gcp_kms", "aws_kms"})


def kek_health_status(
    *, provider: str, envelope_ready: bool, is_deployed: bool,
) -> tuple[dict, bool]:
    """Compute the ``/health`` KEK canary block and whether it DEGRADES service.

    The same "is this deployment production-safe?" judgment the boot gate makes,
    surfaced at ``/health`` instead of at boot.  A ``local`` (development) KEK,
    or an envelope service that failed to initialise (``envelope_ready`` False),
    degrades a DEPLOYED service — credential writes would refuse (503) or run
    without KMS backing.  In development/test it never degrades.

    NEVER leaks a secret: the block carries the provider NAME and booleans only.

    Returns ``(kek_block, degraded)`` where
    ``kek_block = {provider, is_production_grade, envelope_ready}``.
    """
    p = (provider or "").strip().lower() or DEV_KEK_PROVIDER
    is_production_grade = p in PRODUCTION_GRADE_KEK_PROVIDERS
    block = {
        "provider": p,
        "is_production_grade": is_production_grade,
        "envelope_ready": bool(envelope_ready),
    }
    degraded = bool(is_deployed) and (not is_production_grade or not envelope_ready)
    return block, degraded


def validate_boot_safety(settings) -> list[str]:
    """Gate the boot on security-critical settings; fail-closed when deployed.

    Behavior:
      * DEPLOYED env (``NEXUS_ENV`` in ``{staging, production}``) with ANY
        violation ⇒ log each at CRITICAL and RAISE :class:`BootSafetyError`
        (``main.lifespan`` lets it propagate so the process exits).
      * DEVELOPMENT/TEST (or unknown env) ⇒ only WARN; returns the (non-fatal)
        violation list so callers/tests can inspect it.  NEVER raises here, so
        local dev + the existing test suite boot unchanged.
      * No violations ⇒ logs an ``ok`` line and returns ``[]``.

    Returns the violation list (empty when clean).  Raises only in a deployed
    environment with at least one violation.
    """
    nexus_env = getattr(settings, "nexus_env", "development")
    violations = collect_boot_violations(settings)

    if not violations:
        logger.info(
            "qe_central.boot_safety.ok env=%s (no development defaults detected)",
            (nexus_env or "development"),
        )
        return []

    if _is_deployed(nexus_env):
        logger.critical(
            "qe_central.boot_safety.REFUSED env=%s - %d unsafe production "
            "setting(s); refusing to boot:",
            nexus_env,
            len(violations),
        )
        for msg in violations:
            logger.critical("qe_central.boot_safety.violation: %s", msg)
        raise BootSafetyError(violations)

    # development / test / unknown → warn only; never block local dev or tests.
    for msg in violations:
        logger.warning(
            "qe_central.boot_safety.dev_default (non-fatal in env=%s): %s",
            (nexus_env or "development"),
            msg,
        )
    return violations


__all__ = [
    "BootSafetyError",
    "PRODUCTION_GRADE_KEK_PROVIDERS",
    "collect_boot_violations",
    "kek_health_status",
    "validate_boot_safety",
]
