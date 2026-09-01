"""M3.1 — THE VISION GATE AND THE VISION BUDGET (T-VIS-03 / T-VIS-04).

Vision is the most expensive, least verifiable and highest-PII capability in the
crawler.  Before this module it was governed by two booleans borrowed from
elsewhere: an env flag on the SERVER, and — for spend — the MEDIC oracle's call
cap, timeout and breaker.  Two things followed from that, and both are closed
here.

T-VIS-04 · THE DOUBLE GATE IS ``attested AND tenant_enabled``
=============================================================
``crawl_vision_enabled AND tenant.vision_enabled`` is a double gate on the
OPERATOR's intent.  It says nothing about the TARGET.  A tenant who has switched
vision on was therefore free to point a crawl at their live production portal and
have full-page screenshots of real customers' filled applications egress to a
third-party model, because no gate on the vision path had ever looked at the
environment attestation.

So the second half of the gate becomes the ATTESTED TARGET.  The tenant flag
still travels on the dispatch (``ExploreRequest.vision_enabled``), the
attestation is read from the guard context this process was handed, and the
truth table is exhaustive and fail-closed::

    attested   tenant_enabled   vision
    --------   --------------   ------
    no         no               OFF   (no_attestation)
    no         yes              OFF   (no_attestation)   <- the case that ran
    yes        no               OFF   (tenant_disabled)
    yes        yes              ON

THE GATE IS ENFORCED ON THE EXECUTION PATH, not by hiding a flag.  Nothing may
call the perceiver except through a :class:`VisionBudget` built from a
:class:`VisionGate` that returned ``enabled``; the budget refuses every call when
it is not, so a caller that forgets the gate still cannot spend.

WHY ATTESTATION IS A LADDER AND NOT A BOOLEAN.  Two independent attestation
objects exist in this service and they are not interchangeable:

  * ``guard_ctx.walk_authorization`` — built ONLY from an Ed25519 provisioning
    proof this fleet verified against its own trust store and bound to this
    crawl.  Unforgeable by anything downstream of the platform issuer.
  * ``guard_ctx.attestation`` — an UNSIGNED dict that arrived on the dispatch.
    It is trusted for the submit tier only because a human per-flow approval
    stands beside it.

Both are accepted, at named rungs, because refusing the second would make vision
unreachable in every fleet whose issuer half is not yet deployed — and the honest
answer to that is to RECORD WHICH RUNG AUTHORISED, not to pretend the weaker one
is the stronger.  ``VisionGate.rung`` is carried into the evidence, so an audit
reads "vision ran under an unsigned attestation" rather than "vision ran".

T-VIS-03 · AN INDEPENDENT BUDGET, TIMEOUT AND BREAKER
=====================================================
:class:`VisionBudget` owns vision's own cap, its own per-call timeout and its own
consecutive-failure breaker, and nothing else in the crawl shares them.  A canvas
application that exhausts vision therefore leaves the DOM/medic reasoning budget
whole — which was not true before: both oracles decremented
``settings.medic_oracle_max_calls``, so a page that burned ten perceive calls
silently took ten repair calls away from the interaction ladder.

THE BREAKER FAILS CLOSED.  Once open it never re-closes for the life of the
crawl: a vision provider that failed three times in a row is not something to
keep paying to discover, and a half-open probe would reintroduce exactly the
unbounded-spend shape the cap exists to prevent.

Pure except for the injected clock; no I/O, no browser, no HTTP.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Attestation rungs (strongest first) ──────────────────────────────────────

#: A verified, signed, crawl-bound platform provisioning proof (``app.attest``).
RUNG_SIGNED_PROOF = "signed_provisioning_proof"
#: An unsigned dispatch attestation naming a disposable, attributed, unexpired
#: environment (``app.guard.Attestation.is_submit_capable``).
RUNG_DISPOSABLE_ATTESTATION = "disposable_attestation"
#: Nothing attested this target.
RUNG_NONE = "none"

# ── Refusal vocabulary. Every value but OK is a DENY. ────────────────────────

REASON_OK = "ok"
REASON_NO_ATTESTATION = "no_attestation"
REASON_TENANT_DISABLED = "tenant_disabled"
REASON_NO_ORACLE = "no_oracle"

# ── Budget outcomes ──────────────────────────────────────────────────────────

SPEND_OK = "ok"
SPEND_GATE_CLOSED = "gate_closed"
SPEND_CAP_REACHED = "cap_reached"
SPEND_BREAKER_OPEN = "breaker_open"


@dataclass(frozen=True)
class VisionGate:
    """The decision, and everything an auditor needs to re-derive it."""

    enabled: bool
    reason: str
    attested: bool
    tenant_enabled: bool
    rung: str = RUNG_NONE

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "reason": self.reason,
                "attested": self.attested, "tenant_enabled": self.tenant_enabled,
                "attestation_rung": self.rung}


def attestation_rung(
    *, attestation: Any = None, walk_authorization: Any = None,
    now_epoch_ms: Optional[int] = None,
) -> str:
    """Which rung, if any, attests this crawl's TARGET.  Never raises.

    A verified signed proof outranks an unsigned dispatch attestation; anything
    unreadable is :data:`RUNG_NONE`, because an attestation this code cannot
    evaluate is not an attestation.
    """
    if walk_authorization is not None:
        # ``walk_authorization`` exists ONLY when app.attest verified a signed,
        # unexpired, crawl-bound, non-revoked proof — main.py builds it nowhere
        # else. Its presence IS the verification.
        return RUNG_SIGNED_PROOF
    try:
        if attestation is not None and attestation.is_submit_capable(now_epoch_ms):
            return RUNG_DISPOSABLE_ATTESTATION
    except Exception as exc:                     # an unreadable attestation is none
        logger.warning("qec.vision.attestation_unreadable err=%s", str(exc)[:200])
    return RUNG_NONE


def decide_gate(*, attested: bool, tenant_enabled: bool,
                rung: str = RUNG_NONE) -> VisionGate:
    """The T-VIS-04 truth table.  Exhaustive; every combination but
    ``(attested, tenant_enabled)`` is OFF.

    Order of the refusals is deliberate: a missing attestation is reported even
    when the tenant flag is also off, because "we were pointed at an unattested
    target" is the finding an operator needs to see first.
    """
    attested, tenant_enabled = bool(attested), bool(tenant_enabled)
    if not attested:
        return VisionGate(False, REASON_NO_ATTESTATION, attested, tenant_enabled,
                          RUNG_NONE)
    if not tenant_enabled:
        return VisionGate(False, REASON_TENANT_DISABLED, attested, tenant_enabled,
                          rung)
    return VisionGate(True, REASON_OK, True, True, rung)


def gate_for_crawl(
    *, tenant_enabled: bool, attestation: Any = None,
    walk_authorization: Any = None, now_epoch_ms: Optional[int] = None,
) -> VisionGate:
    """:func:`decide_gate` applied to a live crawl's guard context."""
    rung = attestation_rung(attestation=attestation,
                            walk_authorization=walk_authorization,
                            now_epoch_ms=now_epoch_ms)
    return decide_gate(attested=(rung != RUNG_NONE),
                       tenant_enabled=tenant_enabled, rung=rung)


@dataclass
class VisionBudget:
    """Vision's OWN cap, timeout and breaker (T-VIS-03).

    Not a counter with a limit: it is the only object that may authorise a
    vision call, so the gate cannot be bypassed by a caller that forgets it.
    ``max_calls <= 0`` means "no vision calls at all" — a configuration that
    disables the capability without any other switch changing.
    """

    gate: VisionGate
    max_calls: int = 10
    timeout_s: float = 20.0
    breaker_threshold: int = 3

    calls: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    breaker_open: bool = False
    refusals: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0

    def _refuse(self, outcome: str) -> tuple[bool, str]:
        self.refusals[outcome] = self.refusals.get(outcome, 0) + 1
        return False, outcome

    def try_spend(self) -> tuple[bool, str]:
        """Claim one vision call.  ``(True, "ok")`` or ``(False, <reason>)``.

        Fail-closed in every direction: a shut gate, an open breaker and an
        exhausted cap all refuse, and each refusal is COUNTED so a crawl that
        made no vision calls can say which of the three is why.
        """
        if not self.gate.enabled:
            return self._refuse(SPEND_GATE_CLOSED)
        if self.breaker_open:
            return self._refuse(SPEND_BREAKER_OPEN)
        if self.calls >= max(0, int(self.max_calls)):
            return self._refuse(SPEND_CAP_REACHED)
        self.calls += 1
        return True, SPEND_OK

    def note_success(self, latency_ms: int = 0) -> None:
        self.consecutive_failures = 0
        self.latency_ms += max(0, int(latency_ms or 0))

    def note_failure(self, latency_ms: int = 0) -> None:
        """A failed vision call.  Trips — and never un-trips — the breaker."""
        self.failures += 1
        self.consecutive_failures += 1
        self.latency_ms += max(0, int(latency_ms or 0))
        if (not self.breaker_open
                and self.consecutive_failures >= max(1, int(self.breaker_threshold))):
            self.breaker_open = True
            logger.warning(
                "qec.vision.breaker_open failures=%d threshold=%d — no further "
                "vision calls will be attempted on this crawl",
                self.consecutive_failures, self.breaker_threshold)

    @property
    def exhausted(self) -> bool:
        return (not self.gate.enabled or self.breaker_open
                or self.calls >= max(0, int(self.max_calls)))

    def telemetry(self) -> dict[str, Any]:
        """The observability half of T-VIS-03 — independently readable spend."""
        return {
            "gate": self.gate.as_dict(),
            "calls": self.calls,
            "max_calls": int(self.max_calls),
            "timeout_s": float(self.timeout_s),
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "breaker_open": self.breaker_open,
            "breaker_threshold": int(self.breaker_threshold),
            "latency_ms": self.latency_ms,
            "refusals": dict(self.refusals),
        }


def closed_budget(reason: str = REASON_NO_ORACLE) -> VisionBudget:
    """A budget that can never spend — so "vision is off" is ONE object rather
    than a scattering of ``None`` checks at every call site."""
    return VisionBudget(gate=VisionGate(False, reason, False, False, RUNG_NONE),
                        max_calls=0)


__all__ = [
    "RUNG_SIGNED_PROOF", "RUNG_DISPOSABLE_ATTESTATION", "RUNG_NONE",
    "REASON_OK", "REASON_NO_ATTESTATION", "REASON_TENANT_DISABLED",
    "REASON_NO_ORACLE",
    "SPEND_OK", "SPEND_GATE_CLOSED", "SPEND_CAP_REACHED", "SPEND_BREAKER_OPEN",
    "VisionGate", "VisionBudget", "attestation_rung", "decide_gate",
    "gate_for_crawl", "closed_budget",
]
