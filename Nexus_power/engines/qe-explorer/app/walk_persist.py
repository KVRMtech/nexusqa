"""M1.3 — CONTROLLED WALK PERSISTENCE: the per-step budget, the window, and the
immutable audit trail (T-WP-01 / T-WP-03).

WHY THIS EXISTS
===============
``EXPLORE == READ ONLY`` is the assumption the whole guard was built on, and
modern enterprise wizards break it: Save Draft, server-side validation, quote
calculation, eligibility checks and wizard-progress persistence are all writes
that happen during ORDINARY navigation, before anything a human would call a
submit.  A crawler that cannot emit them stops at the first such step, and every
journey behind it is uncatalogued.

WHAT THIS IS NOT
================
It is not a relaxation of the guard.  Walk persistence is a NARROWER grant than
the SUBMIT tier that already exists:

  SUBMIT                                  WALK
  ------------------------------------    ------------------------------------
  unsigned operator attestation           PLATFORM-SIGNED provisioning proof
  human per-flow approval                 no human — so the crypto must be real
  irreversible verbs ALLOWED (recorded)   irreversible verbs NEVER allowed
  burst window per submit                 burst window per ACTUATION, inside a
                                          hard per-logical-step budget
  audit = guard_event                     audit = hash-chained mutation ledger,
                                          written BEFORE the request is released

THE FOUR CONDITIONS (all four, every request, no exceptions)
============================================================
  1. a verified platform attestation (:mod:`app.attest`) — cryptographic;
  2. a disposable environment — carried inside the signed claims, never from
     the dispatch body;
  3. budget available in the CURRENT logical step;
  4. the current logical step has explicitly authorised mutation, and an
     actuation window is open right now.

Any of the four missing is a DENY with a stable reason code.  There is no flag,
env var or dispatch field that can substitute for any of them.

DETERMINISM + THREAD SAFETY
===========================
Every decision is a pure function of (verdict, step state, monotonic now_ms).
No randomness, no wall clock in the hot path (attestation freshness is a
wall-clock question and was already answered once, at crawl start).  All state
transitions hold one re-entrant lock, so the check-and-consume of a budget slot
is atomic and the budget is IMPOSSIBLE to exceed under concurrency.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from .attest import AttestationVerdict, normalize_origin

logger = logging.getLogger(__name__)

#: Genesis link of the audit hash chain.  A chain that starts anywhere else is
#: a chain from a different crawl.
AUDIT_GENESIS = "0" * 64


class WalkReason:
    """Stable reason codes for every walk-persistence authorisation decision."""

    OK = "ok"
    NOT_ATTESTED = "not_attested"
    NO_STEP = "no_step_open"
    STEP_NOT_AUTHORIZED = "step_not_authorized"
    WINDOW_CLOSED = "window_closed"
    BUDGET_EXCEEDED = "budget_exceeded"
    OFF_ORIGIN = "off_origin"
    AUDIT_FAILED = "audit_failed"


# --- The per-logical-step mutation budget -----------------------------------


@dataclass
class StepMutationBudget:
    """``<=N`` mutations within ``<=T`` ms of the step opening.

    Deliberately the SAME shape as :class:`app.auth.AuthWindow` (the Phase-B
    burst model this extends): a count bound and a time bound, both fail-closed,
    both meaningless until something opens the window.  What is new is that the
    window is keyed to a LOGICAL STEP and resets automatically when the walk
    moves on, so a budget cannot accrue across a journey.

    ``max_mutations=0`` is a valid, fully-closed budget — that is what an
    unauthorised crawl gets.
    """

    max_mutations: int = 0
    window_ms: int = 15_000
    step_key: str = ""
    opened_at_ms: Optional[int] = None
    consumed: int = 0

    def begin(self, step_key: str, now_ms: int) -> None:
        """Open a fresh budget for ``step_key``.  Idempotent per key: re-opening
        the SAME step does not refill it, which is what stops a walk that
        re-observes a step from buying itself another allowance."""
        if self.step_key == str(step_key) and self.opened_at_ms is not None:
            return
        self.step_key = str(step_key)
        self.opened_at_ms = int(now_ms)
        self.consumed = 0

    def end(self) -> None:
        self.step_key = ""
        self.opened_at_ms = None
        self.consumed = 0

    @property
    def remaining(self) -> int:
        return max(0, int(self.max_mutations) - int(self.consumed))

    def would_allow(self, now_ms: int) -> "tuple[bool, str]":
        if self.opened_at_ms is None or not self.step_key:
            return False, WalkReason.NO_STEP
        if int(now_ms) - int(self.opened_at_ms) > int(self.window_ms):
            return False, WalkReason.WINDOW_CLOSED
        if self.consumed >= int(self.max_mutations):
            return False, WalkReason.BUDGET_EXCEEDED
        return True, WalkReason.OK

    def consume(self) -> int:
        self.consumed += 1
        return self.consumed


# --- The immutable audit trail ----------------------------------------------


@dataclass(frozen=True)
class MutationAuditRecord:
    """One permitted mutation, recorded BEFORE the request is released.

    ``entry_hash`` chains over ``prev_hash`` + this record's canonical bytes, so
    removing, reordering or editing any entry breaks every entry after it.  The
    chain is what makes the trail immutable in the only sense that matters
    offline: tamper-EVIDENT, re-derivable by anyone holding the manifest.
    """

    sequence: int
    request_id: str
    timestamp_ms: int
    wall_clock_ms: int
    workflow_id: str
    journey_id: str
    step_index: int
    step_fingerprint: str
    triggering_control: str
    method: str
    endpoint: str
    approval: dict
    budget_consumed: int
    budget_max: int
    prev_hash: str
    entry_hash: str = ""
    response_status: Optional[int] = None

    def payload(self) -> dict:
        """Everything the hash covers.  ``entry_hash`` itself is excluded (it is
        the output) and so is ``response_status`` — which is learned AFTER the
        request is released and is recorded as its own linked record, never by
        mutating this one."""
        return {
            "sequence": self.sequence, "request_id": self.request_id,
            "timestamp_ms": self.timestamp_ms, "wall_clock_ms": self.wall_clock_ms,
            "workflow_id": self.workflow_id, "journey_id": self.journey_id,
            "step_index": self.step_index, "step_fingerprint": self.step_fingerprint,
            "triggering_control": self.triggering_control, "method": self.method,
            "endpoint": self.endpoint, "approval": self.approval,
            "budget_consumed": self.budget_consumed, "budget_max": self.budget_max,
            "prev_hash": self.prev_hash,
        }

    def as_dict(self) -> dict:
        out = self.payload()
        out["entry_hash"] = self.entry_hash
        return out


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def chain_hash(prev_hash: str, payload: dict) -> str:
    """``sha256(prev_hash || canonical(payload))`` — the audit chain link."""
    return hashlib.sha256(
        (prev_hash or AUDIT_GENESIS).encode("ascii") + _canonical(payload)
    ).hexdigest()


def verify_audit_chain(records) -> "tuple[bool, str]":
    """Re-derive a whole chain.  ``(True, "")`` or ``(False, why)``.

    This is the offline check a reviewer runs against the manifest: it needs no
    key and no service, only the records themselves.  Both record shapes are
    covered — the AUTHORISATION entry and the linked RESPONSE entry — because a
    chain that only validates half its links validates nothing."""
    prev = AUDIT_GENESIS
    for i, rec in enumerate(records or ()):
        data = dict(rec)
        claimed = str(data.pop("entry_hash", ""))
        data.pop("type", None)            # manifest discriminator, not signed
        if data.pop("kind", "") == "response":
            payload = {"request_id": data.get("request_id"),
                       "response_status": data.get("response_status"),
                       "prev_hash": data.get("prev_hash")}
        else:
            data.pop("response_status", None)
            payload = data
        if str(payload.get("prev_hash") or "") != prev:
            return False, f"record {i}: prev_hash does not chain"
        if chain_hash(prev, payload) != claimed:
            return False, f"record {i}: entry_hash does not re-derive"
        prev = claimed
    return True, ""


def scrub_endpoint(url: str) -> str:
    """``scheme://host[:port]/path`` with the QUERY STRING DROPPED.

    A wizard persists user answers, and a GET-shaped query on a persistence
    endpoint routinely carries them.  The audit trail has to name the endpoint,
    not record the applicant's date of birth into an immutable ledger."""
    try:
        parts = urlsplit((url or "").strip())
    except (ValueError, TypeError):
        return ""
    origin = normalize_origin(url)
    if not origin:
        return (url or "")[:300]
    return f"{origin}{(parts.path or '/')}"[:300]


class MutationAuditLog:
    """Append-only, hash-chained, fail-closed.

    ``sink`` is called with each record dict.  If it RAISES, :meth:`record`
    raises too — and the caller turns that into a DENY.  "No permitted mutation
    without evidence" is only true if an unwritable audit blocks the mutation
    rather than being logged and stepped over.
    """

    def __init__(self, sink: Optional[Callable[[dict], None]] = None) -> None:
        self._lock = threading.RLock()
        self._sink = sink
        self._records: "list[dict]" = []
        self._head = AUDIT_GENESIS
        self._sequence = 0

    def attach_sink(self, sink: Callable[[dict], None]) -> None:
        """Bind the durable sink after construction.

        The authorisation is built in the request handler, before the crawl's
        emitter exists.  Binding later keeps the ledger fail-closed either way:
        with no sink the records are still chained in memory and still gate the
        mutation, and with a sink they additionally reach the manifest."""
        with self._lock:
            self._sink = sink

    @property
    def head(self) -> str:
        with self._lock:
            return self._head

    @property
    def records(self) -> "list[dict]":
        with self._lock:
            return [dict(r) for r in self._records]

    def record(self, **fields: Any) -> MutationAuditRecord:
        with self._lock:
            draft = MutationAuditRecord(sequence=self._sequence + 1,
                                        prev_hash=self._head, **fields)
            entry = chain_hash(self._head, draft.payload())
            rec = MutationAuditRecord(sequence=draft.sequence,
                                      prev_hash=draft.prev_hash,
                                      entry_hash=entry, **fields)
            data = rec.as_dict()
            if self._sink is not None:
                # The sink runs BEFORE any state is committed: a sink failure
                # must leave sequence, head and records exactly as they were, so
                # a retry cannot fork the chain or skip a sequence number.
                self._sink(dict(data))
            self._sequence = rec.sequence
            self._records.append(data)
            self._head = entry
            return rec

    def record_response(self, request_id: str, status: Optional[int]) -> None:
        """Link an observed response status to an earlier authorisation.

        A SEPARATE record, never an edit of the original: an append-only ledger
        that rewrites entries is not append-only."""
        with self._lock:
            payload = {"request_id": str(request_id or ""),
                       "response_status": None if status is None else int(status),
                       "prev_hash": self._head}
            entry = chain_hash(self._head, payload)
            data = dict(payload)
            data["entry_hash"] = entry
            data["kind"] = "response"
            if self._sink is not None:
                self._sink(dict(data))
            self._records.append(data)
            self._head = entry


# --- The authority the guard consults ---------------------------------------


@dataclass
class WalkAuthorization:
    """The verified grant + the live per-step state.  ONE per crawl.

    Constructed ONLY from an :class:`app.attest.AttestationVerdict`.  There is
    no constructor that takes a boolean, so no caller can assemble an
    authorising instance out of a dispatch field.
    """

    verdict: AttestationVerdict
    audit: MutationAuditLog
    workflow_id: str = ""
    budget: StepMutationBudget = field(default_factory=StepMutationBudget)
    journey_id: str = ""
    step_index: int = 0
    step_fingerprint: str = ""
    step_authorized: bool = False
    window_open: bool = False
    triggering_control: str = ""
    clock_ms: Callable[[], int] = lambda: 0
    wall_clock_ms: Callable[[], int] = lambda: 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_verdict(cls, verdict: AttestationVerdict, *, workflow_id: str,
                     audit: Optional[MutationAuditLog] = None,
                     window_ms: int = 15_000,
                     clock_ms: Optional[Callable[[], int]] = None,
                     wall_clock_ms: Optional[Callable[[], int]] = None,
                     ) -> "Optional[WalkAuthorization]":
        """A :class:`WalkAuthorization` for an AUTHORISING verdict, else ``None``.

        ``None`` is the whole backward-compatibility story: every collaborator
        treats a missing authorisation exactly as it behaved before this feature
        existed, so an unattested crawl is byte-identical to today."""
        if verdict is None or not verdict.authorized:
            return None
        import time as _t
        return cls(
            verdict=verdict, audit=audit or MutationAuditLog(),
            workflow_id=str(workflow_id or ""),
            budget=StepMutationBudget(
                max_mutations=int(verdict.max_mutations_per_step),
                window_ms=int(window_ms)),
            clock_ms=clock_ms or (lambda: 0),
            wall_clock_ms=wall_clock_ms or (lambda: int(_t.time() * 1000)),
        )

    # -- logical-step lifecycle ----------------------------------------------

    def begin_step(self, *, journey_id: str, step_index: int,
                   step_fingerprint: str, now_ms: int) -> None:
        """Enter a logical step.  Resets the budget deterministically."""
        with self._lock:
            self.journey_id = str(journey_id or "")
            self.step_index = int(step_index)
            self.step_fingerprint = str(step_fingerprint or "")
            self.step_authorized = False
            self.window_open = False
            self.triggering_control = ""
            # KEYED ON THE STEP ORDINAL, not on the fingerprint. A step can be
            # RE-IDENTIFIED without being left — answering a revealed question
            # changes the page and therefore its fingerprint while the walk is
            # still standing on the same step. Keying the budget on the
            # fingerprint would hand that step a fresh allowance every time it
            # re-identified, which is a budget that resets on the app's say-so.
            self.budget.begin(f"{self.journey_id}::{self.step_index}", int(now_ms))

    def authorize_step(self, authorized: bool = True) -> None:
        """Declare whether THIS logical step may persist at all.

        The walker sets it True only for a step where it is about to actuate a
        control whose purpose is persistence or forward progression.  A step the
        walk merely observes never becomes mutable."""
        with self._lock:
            self.step_authorized = bool(authorized)

    def end_step(self) -> None:
        with self._lock:
            self.step_fingerprint = ""
            self.step_authorized = False
            self.window_open = False
            self.triggering_control = ""
            self.budget.end()

    # -- actuation window -----------------------------------------------------

    def open_window(self, control_name: str, now_ms: int) -> None:
        """Open the narrow window around ONE actuation.

        Outside it, a mutating request is blocked even on a fully attested,
        fully budgeted step — which is what keeps background autosave,
        analytics beacons and co-located forms out of the grant."""
        with self._lock:
            if not self.step_authorized:
                return
            self.window_open = True
            self.triggering_control = str(control_name or "")[:120]

    def close_window(self) -> None:
        with self._lock:
            self.window_open = False
            self.triggering_control = ""

    # -- the decision ---------------------------------------------------------

    def authorize_mutation(self, method: str, url: str, *, now_ms: int
                           ) -> "tuple[bool, str, str]":
        """``(allowed, reason, request_id)``.  Atomic check-and-consume.

        The budget slot is taken INSIDE the lock together with the audit write,
        so two concurrent requests can never both see the last slot.
        """
        with self._lock:
            if self.verdict is None or not self.verdict.authorized:
                return False, WalkReason.NOT_ATTESTED, ""
            if not self.step_authorized:
                return False, WalkReason.STEP_NOT_AUTHORIZED, ""
            if not self.window_open:
                return False, WalkReason.WINDOW_CLOSED, ""
            # The proof names ONE origin; a mutation aimed anywhere else is
            # outside the environment the platform attested as disposable.
            if normalize_origin(url) != self.verdict.target_origin:
                return False, WalkReason.OFF_ORIGIN, ""
            ok, why = self.budget.would_allow(now_ms)
            if not ok:
                return False, why, ""

            consumed = self.budget.consume()
            request_id = self._request_id(consumed)
            try:
                self.audit.record(
                    request_id=request_id,
                    timestamp_ms=int(now_ms),
                    wall_clock_ms=int(self.wall_clock_ms()),
                    workflow_id=self.workflow_id,
                    journey_id=self.journey_id,
                    step_index=int(self.step_index),
                    step_fingerprint=self.step_fingerprint,
                    triggering_control=self.triggering_control,
                    method=(method or "").strip().upper(),
                    endpoint=scrub_endpoint(url),
                    approval=self.verdict.as_audit_dict(),
                    budget_consumed=consumed,
                    budget_max=int(self.budget.max_mutations),
                )
            except Exception:
                # EVIDENCE OR NOTHING.  The slot stays consumed on purpose: a
                # failing audit sink must not become a way to retry a mutation
                # until the write happens to succeed.
                logger.exception("qec.walk.audit_write_failed — refusing the mutation")
                return False, WalkReason.AUDIT_FAILED, ""
            return True, WalkReason.OK, request_id

    def attach(self, *, sink: Optional[Callable[[dict], None]] = None,
               wall_clock_ms: Optional[Callable[[], int]] = None) -> None:
        """Bind the crawl's emitter + clock once the Crawler exists."""
        with self._lock:
            if sink is not None:
                self.audit.attach_sink(sink)
            if wall_clock_ms is not None:
                self.wall_clock_ms = wall_clock_ms

    def note_response(self, request_id: str, status: Optional[int]) -> None:
        if not request_id:
            return
        try:
            self.audit.record_response(request_id, status)
        except Exception:      # pragma: no cover - never break a live crawl here
            logger.exception("qec.walk.audit_response_failed")

    # -- reporting ------------------------------------------------------------

    def _request_id(self, consumed: int) -> str:
        """Deterministic — the SAME crawl replayed produces the SAME ids, which
        is what lets an evidence reviewer diff two runs."""
        seed = f"{self.workflow_id}|{self.journey_id}|{self.step_index}|" \
               f"{self.budget.step_key}|{consumed}"
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]

    def summary(self) -> dict:
        with self._lock:
            return {
                "authorized": True,
                "proof_id": self.verdict.proof_id,
                "environment_id": self.verdict.environment_id,
                "kid": self.verdict.kid,
                "max_mutations_per_step": int(self.budget.max_mutations),
                "window_ms": int(self.budget.window_ms),
                "mutations": sum(1 for r in self.audit.records if "method" in r),
                "audit_head": self.audit.head,
            }


def unauthorized_summary(reason: str) -> dict:
    """What ``crawl_meta`` records when walk persistence is NOT granted — the
    honest reason, so a crawl that stopped at a Save Draft says why."""
    return {"authorized": False, "reason": reason or "not_attested",
            "max_mutations_per_step": 0, "mutations": 0}


__all__ = [
    "AUDIT_GENESIS", "MutationAuditLog", "MutationAuditRecord",
    "StepMutationBudget", "WalkAuthorization", "WalkReason", "chain_hash",
    "scrub_endpoint", "unauthorized_summary", "verify_audit_chain",
]
