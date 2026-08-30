"""RUNG 2 — THE CLIENT'S OWN TEST ENVIRONMENT, ANSWERING FOR ITSELF.

WHY THIS RUNG IS THE ENTERPRISE ON-RAMP. Some values cannot be invented by
anyone: a member id that exists, a policy in force in Texas, the fixed OTP a
test environment issues, the stub answers to a knowledge-based identity check.
A generator cannot produce them, a model cannot know them, and harvest only
finds them if the crawl has already walked a page that displays them.

The client can answer all of it — and the ask is the thing that decides whether
a platform is adopted. "Send us a spreadsheet of test data" is where every
incumbent's onboarding dies. "Give us a read-only URL and a token, once" is a
different conversation entirely.

ONE RUNG, SEVERAL DOORS. Clients differ in what they can open, and the rung
does not care which:

  * ``rest``     a URL this service queries per field (a published contract);
  * ``manifest`` a static export the client uploads once (no network at all);
  * ``mcp``      an MCP endpoint, for clients who already speak it.

All three satisfy the same protocol, so the ladder has ONE rung rather than
three, and adding a fourth door later changes nothing above this line.

WHAT THIS MODULE IS AND IS NOT. It is the resolution core: which slot a field
maps to, and how a provider's answer becomes a value. It is PURE — no network,
no database — so the collision rules can be proven without either. The
transports live in :mod:`app.services.env_data_transports`, and the tenant
credential belongs in the existing sealed store, not here.

SAFETY, INHERITED RATHER THAN REINVENTED. Slot resolution reuses
:mod:`app.services.data_library`'s rule, which refuses to carry a value when a
field ambiguously matches two slots — the guarantee that keeps a test SSN out of
a routing-number field. This module adds no looser path around it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, Sequence

from . import data_library

logger = logging.getLogger(__name__)

#: How the client's environment is reached. The rung is the same either way.
KIND_REST = "rest"
KIND_MANIFEST = "manifest"
KIND_MCP = "mcp"
PROVIDER_KINDS = (KIND_REST, KIND_MANIFEST, KIND_MCP)

#: Resolution outcomes, mirroring data_library's vocabulary so a reader moving
#: between the two files meets one set of words.
ANSWERED = "ANSWERED"
ASK = "ASK"

R_NO_PROVIDER = "no environment provider is configured for this tenant"
R_NO_SLOT = "no environment slot matches this field — stays ASK"
R_AMBIGUOUS = "the field matches two slots — refusing to guess which"
R_EMPTY = "the environment holds no value for that slot"
R_UNAVAILABLE = "the environment could not be reached"


class EnvProvider(Protocol):
    """What a client's environment must offer. Three doors, one shape.

    ``slots()`` names what this environment can answer — the same
    human-assigned semantic keys the data library uses, so a client who has
    already populated one is not asked to learn a second vocabulary.

    ``value(slot_key)`` returns the value or ``None``. It must NEVER raise: an
    unreachable environment is a rung that declines, not a crawl that stops.
    """

    def slots(self) -> Sequence[str]: ...

    def value(self, slot_key: str) -> Optional[str]: ...


@dataclass(frozen=True)
class EnvAnswer:
    """One resolution, with the reason it went that way."""

    disposition: str
    value: Optional[str] = None
    slot_key: Optional[str] = None
    reason: str = ""


@dataclass
class StaticProvider:
    """A manifest the client exported once. No network, no credential.

    The simplest door, and the one to reach for first with a client who cannot
    open a port: it is a file, it is reviewable, and it works identically to
    the others from the ladder's point of view.
    """

    values: Mapping[str, str] = field(default_factory=dict)

    def slots(self) -> Sequence[str]:
        return [k for k, v in self.values.items() if str(v or "").strip()]

    def value(self, slot_key: str) -> Optional[str]:
        got = str(self.values.get(slot_key) or "").strip()
        return got or None


def resolve(field_label: str, provider: Optional[EnvProvider]) -> EnvAnswer:
    """Resolve one field against the client's environment. PURE.

    Returns ANSWERED only when EXACTLY ONE slot matches and it holds a value.
    Every other path is ASK with a reason a human can act on — an ambiguous
    match is refused rather than guessed, because carrying the wrong secret into
    a field is the one failure this rung must never produce.
    """
    if provider is None:
        return EnvAnswer(ASK, reason=R_NO_PROVIDER)

    label = str(field_label or "").strip()
    if not label:
        return EnvAnswer(ASK, reason=R_NO_SLOT)

    try:
        keys = list(provider.slots() or ())
    except Exception:                                            # noqa: BLE001
        logger.info("qec.env_data.slots_unavailable")
        return EnvAnswer(ASK, reason=R_UNAVAILABLE)

    matches = data_library.matching_slots(label, keys)
    if not matches:
        return EnvAnswer(ASK, reason=R_NO_SLOT)
    if len(matches) > 1:
        # The same refusal the data library makes, for the same reason: a
        # 9-digit SSN and a 9-digit routing number must never be interchanged.
        return EnvAnswer(ASK, reason=R_AMBIGUOUS)

    slot = matches[0]
    try:
        got = provider.value(slot)
    except Exception:                                            # noqa: BLE001
        logger.info("qec.env_data.value_unavailable slot=%r", slot[:40])
        return EnvAnswer(ASK, slot_key=slot, reason=R_UNAVAILABLE)

    if got is None or not str(got).strip():
        return EnvAnswer(ASK, slot_key=slot, reason=R_EMPTY)
    return EnvAnswer(ANSWERED, value=str(got), slot_key=slot)


def answer_key_overlay(provider: Optional[EnvProvider],
                       labels: Sequence[str]) -> dict[str, str]:
    """Resolve many fields at once into an answer-key-shaped overlay.

    The explorer already understands an answer key, so an environment's answers
    reach a crawl as an extension of one rather than as a new concept the
    crawler has to learn. Only unambiguous, non-empty answers are included —
    everything else simply stays out and falls to the rungs below.

    The VALUES are the client's own. This function returns them to the caller
    that dispatches the crawl and nothing here logs one.
    """
    overlay: dict[str, str] = {}
    for label in labels or ():
        answer = resolve(label, provider)
        if answer.disposition == ANSWERED and answer.value is not None:
            overlay[str(label)] = answer.value
    if overlay:
        logger.info("qec.env_data.overlay fields=%d", len(overlay))
    return overlay
