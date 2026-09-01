"""THE KEY A REMEMBERED VALUE IS STORED UNDER.

Field learning worked exactly once.  Everything a crawl learned was written
under the ARTIFACT it produced — ``tp_field_memory`` is keyed
``(tenant_id, artifact_id, signature)`` and the ciphertext is bound to that
artifact through its AAD — and a re-crawl MINTS A NEW ARTIFACT.  So crawl N
stored its answers under artifact N, crawl N+1 read artifact N (the "latest
completed" one) and wrote to artifact N+1, and crawl N+2 could no longer see
anything crawl N had learned.  Each crawl inherited exactly one generation of
memory and then dropped it.

The identity had the same defect through the same key: ``identity_seed`` came
back as ``tenant::artifact``, so the applicant CHANGED between runs.  A rate
quote that moves because the age moved is a false difference, and there is no
way to tell it from a real one after the fact.

Both are one mistake — a per-RUN key used for per-APPLICATION knowledge — so
both are fixed here, in one place, with one function:

    :func:`memory_scope`   the stable scope string for a tenant + application.

WHAT MAKES A SCOPE SAFE
    * it contains the TENANT, so one client's remembered values can never
      resolve for another;
    * it contains the APPLICATION, so two applications belonging to one tenant
      keep separate people and separate memories;
    * it contains a VERSION, so a future change to what a scope means can
      invalidate old rows by construction rather than by silently mismatching
      them — the same discipline as ``field_signature.SIGNATURE_VERSION``;
    * it contains NO artifact, NO crawl id, NO timestamp and NO value, so it is
      stable across runs, which is the whole point.

The separator is escaped, because a tenant id containing the separator could
otherwise be crafted to collide with another tenant's scope — a cross-tenant
read is the one failure here that is not recoverable.

PURE + DETERMINISTIC.  No I/O, no clock.
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional

__all__ = ["memory_scope", "identity_seed", "scope_digest", "MEMORY_SCOPE_VERSION",
           "parse_scope"]

#: Bumped only when the MEANING of a scope changes in a way that must invalidate
#: everything stored under the old one.  v1 is ``tenant + application``.
MEMORY_SCOPE_VERSION = 1

_SEPARATOR = "::"
#: Anything that is not an unambiguous identifier character is percent-escaped,
#: so ``a::b`` as a tenant id can never masquerade as tenant ``a``, app ``b``.
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _escape(part: str) -> str:
    return _UNSAFE_RE.sub(lambda m: f"%{ord(m.group(0)):02X}", str(part or ""))


def memory_scope(tenant_id: str, app_id: str, *,
                 version: int = MEMORY_SCOPE_VERSION) -> str:
    """The stable scope a tenant's learning and identity live under.

    ``app_id`` is the APPLICATION — the thing that is the same on every crawl —
    and never an artifact, a crawl id or an exploration id.  A caller that only
    has an artifact id is holding a per-run handle and must resolve the
    application first; passing it here would reproduce the defect this function
    exists to remove, so an empty ``app_id`` raises rather than silently
    producing a scope that looks fine and forgets everything."""
    tenant = str(tenant_id or "").strip()
    app = str(app_id or "").strip()
    if not tenant:
        raise ValueError("a memory scope needs a tenant: an unscoped scope "
                         "would let one client read another's values")
    if not app:
        raise ValueError("a memory scope needs an application id, not an "
                         "artifact id: an artifact changes every crawl, which "
                         "is exactly how learning came to expire after one run")
    return f"v{int(version)}{_SEPARATOR}{_escape(tenant)}{_SEPARATOR}{_escape(app)}"


def parse_scope(scope: str) -> Optional[tuple[int, str, str]]:
    """``(version, tenant, app)`` from a scope string, or ``None``.

    Used by the migration that re-keys existing rows, and by a diagnostic that
    needs to say which application a stored row belongs to."""
    parts = str(scope or "").split(_SEPARATOR)
    if len(parts) != 3 or not parts[0].startswith("v"):
        return None
    try:
        version = int(parts[0][1:])
    except ValueError:
        return None
    return version, _unescape(parts[1]), _unescape(parts[2])


def _unescape(part: str) -> str:
    return re.sub(r"%([0-9A-Fa-f]{2})",
                  lambda m: chr(int(m.group(1), 16)), part)


def identity_seed(tenant_id: str, app_id: str) -> str:
    """The seed the crawl's synthetic household is derived from.

    THE SAME SCOPE AS THE MEMORY, deliberately.  The person and the values
    remembered about that person have to change together or not at all: an
    identity that rotates while the memory persists produces a form filled with
    one person's remembered postcode and another person's name, which validates
    worse than either alone."""
    return memory_scope(tenant_id, app_id)


def scope_digest(scope: str) -> str:
    """A short, stable digest of a scope.

    For log lines and metric labels: it identifies a scope across runs without
    putting a tenant id in a log.  Never used as a storage key — a digest cannot
    be parsed back into the tenant it belongs to, and a storage key that cannot
    be audited is one nobody can prove is correctly isolated."""
    return hashlib.sha256(str(scope or "").encode("utf-8")).hexdigest()[:16]
