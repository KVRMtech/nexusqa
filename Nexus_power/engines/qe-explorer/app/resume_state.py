"""DURABLE RESUME — a killed crawl continues; it does not start again (M1.7 /
T-GW-03).

THE HOLE THIS CLOSES, and why "resume infrastructure exists" was not the same as
"resume works".

Two halves of a resume shipped, and only one of them was ever built.

The RECOVERY half is real: ``emit.scan_resume_state`` reads the durable manifest
prefix and seeds ``_visited_fingerprints``, the sequence index, the frame index
and the clock offset.  That code is correct and this module does not replace it.

The CONTINUATION half was never written.  ``scan_resume_state`` restores what the
crawl HAD SEEN and nothing about what it STILL HAD TO DO.  The frontier — the
entire work list — is rebuilt empty on every start.  So a resumed crawl:

    seeds visited = {root_fp, ...}
        -> pushes only the entry URL
        -> _expand observes the root
        -> `if fingerprint in self._visited_fingerprints: return`
        -> frontier is now empty
        -> _explore_loop returns
        -> stop_reason = "completed", states = 0

The visited set, restored faithfully, is precisely what makes the resumed crawl
terminate immediately — the recovery half actively causes the zero-state
completion.  And because qe-central mints a fresh ``crawl_id`` on every dispatch
(``uuid.uuid4().hex`` in ``routers/explorations.py``), the manifest a resume
would read is under the OLD id and is never opened at all.  Resume was
unreachable from the top and self-defeating at the bottom.

THE FIX, in three parts:

  1. **A checkpoint record** (:data:`REC_CHECKPOINT`) written into the same
     append-only manifest, carrying the FRONTIER — the work list — alongside the
     counters.  Evidence and work-list are then one durable object with one
     crash-consistency story; there is no second file to get out of step.
  2. **Rebuild** (:func:`rebuild`) that reconstructs a :class:`ResumePlan` from
     the last complete checkpoint plus the page-state prefix.
  3. **An honest refusal** (:attr:`ResumePlan.recoverable`) when a resume was
     asked for and the prefix cannot support one.  The crawl fails as
     ``resume_unrecoverable``; it never silently becomes a zero-state success.

WHY THE LAST CHECKPOINT AND NOT A FOLD OF ALL OF THEM.  A checkpoint is a
SNAPSHOT of the frontier, not a delta, so the newest complete one is the whole
truth; folding earlier ones back in would resurrect work the crawl has since
finished.  ``emit.read_records`` already truncates at the first partial line, so
the last checkpoint this module sees is by construction a complete one.

PURE.  No I/O: callers hand it records and it hands back a plan.  Everything here
unit-tests against a list of dicts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

#: The manifest record type this module owns.  Additive: every existing reader
#: (``map_manifest_records_to_bundle``, the characterization goldens) dispatches
#: on ``type`` and ignores what it does not know, so a manifest carrying
#: checkpoints maps to a byte-identical bundle.
REC_CHECKPOINT = "checkpoint"

#: How many frontier items a checkpoint may carry.  A crawl bounded to a few
#: hundred states cannot have a legitimately larger work list, and an unbounded
#: field would let a pathological app write a manifest line big enough to make
#: the whole prefix unreadable.
MAX_CHECKPOINT_FRONTIER = 2_000


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class FrontierSnapshot:
    """One queued frontier item, in the manifest's flat wire shape.

    Mirrors :class:`app.frontier.FrontierItem` field for field.  Kept as its own
    type rather than serialising the crawler's dataclass directly so a change to
    the in-memory item cannot silently change a persisted manifest's schema.
    """

    url: str
    depth: int = 0
    priority: int = 0
    discovered_via: str = ""
    parent_fingerprint: str = ""
    #: The REACH KEY this item was deduped on (``url_template``), stamped by
    #: :meth:`app.frontier.Frontier.push`.  It travels with the item because the
    #: queue and the dedup set are ONE consistent object: restoring the queue by
    #: URL and the dedup set by key would let a restored item be rejected by its
    #: own key, which is a queue that silently loses work.
    key: str = ""

    def as_dict(self) -> dict:
        return {"url": self.url, "depth": self.depth, "priority": self.priority,
                "discovered_via": self.discovered_via,
                "parent_fingerprint": self.parent_fingerprint, "key": self.key}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Optional["FrontierSnapshot"]:
        """Parse one persisted item; ``None`` when it cannot be trusted.

        A frontier item with no URL is not a work item — it is corruption, and
        admitting it would put an un-navigable entry back on the queue.
        """
        url = str(raw.get("url") or "").strip()
        if not url:
            return None
        return cls(
            url=url[:2000],
            depth=_int(raw.get("depth")),
            priority=_int(raw.get("priority")),
            discovered_via=str(raw.get("discovered_via") or "")[:300],
            parent_fingerprint=str(raw.get("parent_fingerprint") or "")[:64],
            key=str(raw.get("key") or "")[:2000],
        )


def build_checkpoint(
    *, frontier: Sequence[Any], visited: Iterable[str], states: int, actions: int,
    spent_keys: Iterable[str] = (), sequence_index: int = 0,
) -> dict:
    """Build the checkpoint RECORD for the current crawl position.

    ``spent_keys`` is the frontier's push-time dedup set.  It has to travel: a
    resume that restored the queue but not the spent keys would re-enqueue every
    URL the crawl had already dequeued the moment one was rediscovered, and walk
    the app a second time inside the same crawl id.
    """
    items = []
    for item in list(frontier)[:MAX_CHECKPOINT_FRONTIER]:
        snapshot = FrontierSnapshot(
            url=str(getattr(item, "url", "") or ""),
            depth=_int(getattr(item, "depth", 0)),
            priority=_int(getattr(item, "priority", 0)),
            discovered_via=str(getattr(item, "discovered_via", "") or ""),
            parent_fingerprint=str(getattr(item, "parent_fingerprint", "") or ""),
            key=str(getattr(item, "key", "") or ""),
        )
        if snapshot.url:
            items.append(snapshot.as_dict())
    return {
        "type": REC_CHECKPOINT,
        "frontier": items,
        "frontier_truncated": len(list(frontier)) > MAX_CHECKPOINT_FRONTIER,
        "spent_keys": sorted({str(k) for k in spent_keys if str(k)})[:MAX_CHECKPOINT_FRONTIER],
        "visited_count": sum(1 for _ in visited),
        "states": _int(states),
        "actions": _int(actions),
        "sequence_index": _int(sequence_index),
    }


@dataclass(frozen=True)
class ResumePlan:
    """What a resumed run must do to CONTINUE the crawl it inherited.

    ``recoverable`` is the load-bearing field and its default is the honest one:
    a plan built from nothing is not recoverable, so a caller that forgets to
    check it cannot accidentally treat an empty inheritance as a fresh start.
    """

    #: Frontier items to re-queue, in their persisted order.
    frontier: tuple = ()
    #: Reach keys already spent, so nothing is walked twice inside one crawl id.
    spent_keys: frozenset = frozenset()
    #: page_state records already durable under this crawl id.
    prior_states: int = 0
    #: action records already durable under this crawl id.
    prior_actions: int = 0
    #: True when a checkpoint was found (as opposed to a bare page-state prefix).
    has_checkpoint: bool = False
    #: True when the prefix supports CONTINUING.  See :func:`rebuild`.
    recoverable: bool = False
    #: Operator-facing reason a resume cannot proceed; "" when it can.
    refusal: str = ""

    def as_dict(self) -> dict:
        return {"frontier": len(self.frontier), "spent_keys": len(self.spent_keys),
                "prior_states": self.prior_states, "prior_actions": self.prior_actions,
                "has_checkpoint": self.has_checkpoint,
                "recoverable": self.recoverable, "refusal": self.refusal}


#: A resume was requested for a crawl id that has no durable evidence at all.
#: NOT the same as "a new crawl": the dispatcher asked us to continue something,
#: and the something is not there.  Failing here is what stops a lost manifest
#: (a wiped volume, a wrong mount) from being reported as a completed re-crawl.
REFUSAL_NO_PREFIX = (
    "resume requested but this crawl id has no durable manifest prefix — there is "
    "nothing to continue (lost or unmounted evidence volume?)"
)

#: The prefix exists and is exhausted: states were recorded and no work remains.
#: This is a resume of an ALREADY-FINISHED crawl and it is not an error — the
#: run completes on its inherited evidence without re-walking anything.
REFUSAL_NONE = ""


def rebuild(records: Iterable[Mapping[str, Any]], *, resuming: bool) -> ResumePlan:
    """Reconstruct the continuation plan from a durable manifest prefix.  PURE.

    ``resuming`` is the DISPATCHER'S INTENT, and it changes only the refusal, not
    the reconstruction — a fresh crawl whose id happens to own a prefix still
    inherits it (that is what makes an accidental re-dispatch of a live crawl id
    additive rather than destructive), but only an explicit resume is failed when
    the prefix is missing.

    The three outcomes:

      * **no prefix at all** — a fresh crawl (``recoverable`` is irrelevant, the
        caller starts normally); an explicit resume is REFUSED.
      * **a prefix with work left** — ``recoverable``, frontier restored, the run
        continues past the durable prefix.
      * **a prefix with no work left** — ``recoverable`` with an empty frontier:
        the crawl was already finished, and its inherited ``prior_states`` are
        what keep :func:`app.completion.adjudicate` from calling it evidence-free.
    """
    from .emit import REC_ACTION, REC_PAGE_STATE  # local: keeps this module leaf-ish

    prior_states = 0
    prior_actions = 0
    checkpoint: Optional[Mapping[str, Any]] = None
    for rec in records or ():
        rtype = rec.get("type")
        if rtype == REC_PAGE_STATE:
            prior_states += 1
        elif rtype == REC_ACTION:
            prior_actions += 1
        elif rtype == REC_CHECKPOINT:
            checkpoint = rec          # last one wins: a checkpoint is a snapshot

    if prior_states == 0 and checkpoint is None:
        return ResumePlan(
            recoverable=not resuming,
            refusal=REFUSAL_NO_PREFIX if resuming else REFUSAL_NONE,
        )

    frontier: list[FrontierSnapshot] = []
    spent: set[str] = set()
    if checkpoint is not None:
        for raw in (checkpoint.get("frontier") or ())[:MAX_CHECKPOINT_FRONTIER]:
            if not isinstance(raw, Mapping):
                continue
            snapshot = FrontierSnapshot.from_mapping(raw)
            if snapshot is not None:
                frontier.append(snapshot)
        spent = {str(k) for k in (checkpoint.get("spent_keys") or ()) if str(k)}

    # THE QUEUED ITEMS ARE NOT SPENT.  ``spent_keys`` is the push-time dedup set,
    # which by construction contains a key for every item ever queued INCLUDING
    # the ones still waiting.  Restoring the dedup set first and then re-pushing
    # the queue would have every restored item rejected by its own key, and the
    # resume would come back with an empty frontier — the zero-state completion,
    # rebuilt by the very code meant to prevent it.  So the still-queued keys are
    # subtracted here: the caller re-arms them by PUSHING, and marks only the
    # genuinely-consumed remainder.
    queued_keys = {snapshot.key or snapshot.url for snapshot in frontier}
    spent -= queued_keys

    return ResumePlan(
        frontier=tuple(frontier),
        spent_keys=frozenset(spent),
        prior_states=prior_states,
        prior_actions=prior_actions,
        has_checkpoint=checkpoint is not None,
        recoverable=True,
        refusal=REFUSAL_NONE,
    )


__all__ = [
    "REC_CHECKPOINT", "MAX_CHECKPOINT_FRONTIER", "FrontierSnapshot",
    "ResumePlan", "build_checkpoint", "rebuild",
    "REFUSAL_NO_PREFIX", "REFUSAL_NONE",
]
