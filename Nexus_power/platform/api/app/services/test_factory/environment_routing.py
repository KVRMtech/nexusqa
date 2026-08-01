"""Environment routing — the selected environment decides WHERE a run goes.

A run names an ``environment_id``, and that name has been used to pick the
credential card, the posture, the reservation and the member-data answers — but
never the ADDRESS. The destination came from the request body, which the portal
defaults to the origin that was crawled. So an operator could select ``uat`` and
have the traffic land on whatever host the crawl happened to use.

That is not merely a routing bug. Posture is enforced from the registered
environment row, so a production environment correctly marked default-deny is
consulted only when its LABEL is selected. Select ``uat`` while the address still
points at the production host and the guard reads uat's posture, allows the run,
and a mutating suite executes against production — while the screen, the report,
the parity trend and the certification ledger all say ``uat``.

Two rules follow, and this module exists to make them impossible to skip:

  * **the registered environment is the source of truth for the destination.** If
    an environment is registered with a base_url, the run goes there.
  * **posture is judged on the destination, never on the label.** A caller-supplied
    address that disagrees with the registered one is a CONFLICT and is refused —
    silently preferring either one would let the two diverge again.

An UNREGISTERED environment_id is refused rather than defaulted. Treating an
unknown target as "read_write, non-production" is the most permissive possible
reading of a target nobody described.

Pure — no DB, no I/O. The caller loads the environment row and applies the result.
"""
from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["resolve_destination", "same_origin", "origin_of"]


def origin_of(url: object) -> str:
    """scheme://host[:port], lowercased. '' when not an absolute http(s) URL."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}".lower()


def same_origin(a: object, b: object) -> bool:
    """Do two URLs address the same host? Paths may differ — a suite entering at
    /portal/apply on the same host as the registered base_url is not a conflict."""
    origin_a, origin_b = origin_of(a), origin_of(b)
    return bool(origin_a) and origin_a == origin_b


def resolve_destination(*, environment_id: str, environment: dict | None,
                        requested_base_url: str = "",
                        requested_env_context: dict | None = None) -> dict:
    """Decide where this run goes, and whether it may go at all.

    ``environment_id``        what the operator selected ('' = none selected)
    ``environment``           the REGISTERED row (persona_store.get_environment), or None
    ``requested_base_url``    the caller's address, if any
    ``requested_env_context`` the caller's routing context, if any

    Returns::

        {allowed, base_url, env_context, environment_id, source, reason, detail}

    ``source`` says which authority set the address — ``environment`` (registered),
    ``request`` (no environment selected), or ``none``. It is carried into the run
    so a report can state how the destination was decided rather than implying it.
    """
    env = environment or {}
    requested_origin = origin_of(requested_base_url) or origin_of(
        (requested_env_context or {}).get("base_url"))

    # ── No environment selected: today's behaviour, unchanged. ────────────────
    # The caller's address stands. Nothing is claimed about which environment was
    # exercised, so nothing can be misreported.
    if not str(environment_id or "").strip():
        return {
            "allowed": True,
            "base_url": str(requested_base_url or "").strip(),
            "env_context": dict(requested_env_context or {}),
            "environment_id": "",
            "source": "request" if requested_origin else "none",
            "reason": "",
            "detail": {},
        }

    # ── An environment WAS selected but is not registered. ────────────────────
    # Refused, not defaulted: an unregistered target has no posture, no owner and
    # no routing, so allowing it means running against something nobody described
    # while the report names an environment that does not exist.
    if not env:
        return {
            "allowed": False,
            "base_url": "", "env_context": {}, "environment_id": environment_id,
            "source": "none",
            "reason": "environment_not_registered",
            "detail": {
                "environment_id": environment_id,
                "note": ("This run names an environment that is not registered, so "
                         "there is nothing to say where it should go or whether it "
                         "may be written to. Register it in Members & Environments, "
                         "or clear the environment to run against an explicit URL. "
                         "This is a configuration gap, NOT an application failure."),
            },
        }

    registered = str(env.get("base_url") or "").strip()

    # ── Registered, but with no address of its own. ───────────────────────────
    # Posture still governs; the caller's address stands because the environment
    # never claimed one. Surfaced so the operator can see the environment is only
    # half-described.
    if not registered:
        return {
            "allowed": True,
            "base_url": str(requested_base_url or "").strip(),
            "env_context": dict(requested_env_context or {}),
            "environment_id": environment_id,
            "source": "request",
            "reason": "environment_has_no_base_url",
            "detail": {"environment_id": environment_id},
        }

    # ── The caller also supplied an address. It must AGREE. ───────────────────
    # Preferring the environment silently would make a pinned address a lie;
    # preferring the request silently is the original defect. A disagreement is a
    # conflict the operator has to resolve.
    if requested_origin and not same_origin(requested_origin, registered):
        return {
            "allowed": False,
            "base_url": "", "env_context": {}, "environment_id": environment_id,
            "source": "none",
            "reason": "destination_conflicts_with_environment",
            "detail": {
                "environment_id": environment_id,
                "registered_origin": origin_of(registered),
                "requested_origin": requested_origin,
                "note": ("The selected environment and the requested address point at "
                         "different hosts. Running would exercise one while the report "
                         "named the other — including its production posture. Clear the "
                         "address to use the environment, or select the environment that "
                         "matches it."),
            },
        }

    # ── Resolved: the environment decides. ───────────────────────────────────
    # Routing fields come from the environment too, so a cookie- or header-selected
    # lane is carried rather than dropped onto the host's default (production).
    context = dict(requested_env_context or {})
    context["environment_id"] = environment_id
    context["base_url"] = registered
    for key in ("cookies", "headers", "http_credentials", "data_overrides",
                "env_assertion", "data_epoch"):
        value = env.get(key)
        if value:
            context[key] = value
    return {
        "allowed": True,
        "base_url": registered,
        "env_context": context,
        "environment_id": environment_id,
        "source": "environment",
        "reason": "",
        "detail": {"registered_origin": origin_of(registered)},
    }
