"""The two LIFECYCLE EDGES of a crawl: how it resumes, and how it ends.

Extracted from ``crawler.py`` (Gate 0 · task 12), which stood at 1294 LOC
against a <900 exit target. A PURE RELOCATION: not one character of logic,
ordering or naming changed, so the characterization goldens are byte-identical
by construction rather than by re-baselining.

``CheckpointMixin`` re-queues what a previous run of the same crawl id left
behind (M1.7 / T-GW-03); ``FinishMixin`` adjudicates the terminal verdict from
EVIDENCE and builds the summary (T-GW-01/03). They sit together because both
are edges rather than steps — nothing in the walk loop calls either.
"""
from __future__ import annotations

import logging

from . import completion
from . import emit
from . import resume_state
from .crawl_constants import STOP_COMPLETED, _url_key
from .crawl_summary import CrawlSummary
from .frontier import FrontierItem

logger = logging.getLogger(__name__)


class CheckpointMixin:
    """Durable resume — re-queue a prior run's unfinished frontier."""

    # -- durable resume (M1.7 / T-GW-03) --------------------------------------

    def _restore_frontier(self) -> int:
        """Re-queue the work list a previous run of THIS crawl id left behind.

        The half of a resume that was never written.  ``emit.scan_resume_state``
        restores what the crawl had SEEN; without this, what it still had TO DO
        was rebuilt empty on every start - so the restored visited-set matched
        the freshly-observed entry page, ``_expand`` took its unique-state early
        return, the frontier drained, and the run reported ``completed`` with
        zero states.  The recovery half actively CAUSED the zero-state
        completion; this is the missing counterweight.

        Order matters and is the reason this is a method rather than three lines
        at the call site:

          1. **Push the queued items first.**  Each carries the reach key it was
             deduped under, so pushing re-arms exactly the key it owns.
          2. **Then mark the consumed keys.**  These are routes an earlier run
             already dequeued and expanded.  Marking them AFTER the pushes is
             what stops a restored item from being rejected by a dedup set that
             already contains its own key - which would silently drop the work
             this method exists to restore.

        Ordering within the queue is deliberately NOT restored from the file:
        :meth:`app.frontier.Frontier.push` recomputes novelty rank and plan
        priority from the key, so re-pushing reproduces the same traversal order
        the original run would have had.  The snapshot only has to be COMPLETE,
        not ordered.

        Returns how many items were re-queued (0 for a fresh crawl).
        """
        plan = self._resume_plan
        if not plan.frontier and not plan.spent_keys:
            return 0
        requeued = 0
        for snapshot in plan.frontier:
            item = FrontierItem(
                url=snapshot.url, depth=snapshot.depth, priority=snapshot.priority,
                discovered_via=snapshot.discovered_via,
                parent_fingerprint=snapshot.parent_fingerprint,
            )
            if self._frontier.push(item, key=snapshot.key or _url_key(snapshot.url)):
                requeued += 1
        marked = self._frontier.mark_spent(plan.spent_keys)
        logger.warning(
            "qec.crawler.resume_restored crawl_id=%s requeued=%d spent_keys=%d "
            "prior_states=%d prior_actions=%d - this run CONTINUES the crawl; it "
            "does not start a new one",
            self.crawl_id, requeued, marked, plan.prior_states, plan.prior_actions)
        return requeued

    def _emit_checkpoint(self) -> None:
        """Persist the current work list into the durable manifest.

        Written after every expansion, into the SAME append-only file the
        evidence goes to.  One file means one crash-consistency story: a
        checkpoint can never describe a moment the evidence does not, because
        both are ordered by the same fsynced appends and
        ``emit.read_records`` truncates both at the same partial line.

        Best-effort by construction.  A checkpoint that cannot be written costs a
        resume some re-walking; a checkpoint that KILLS a running crawl costs the
        whole crawl.  The evidence records are the ones whose write failures are
        allowed to propagate.
        """
        try:
            self._emitter.emit_checkpoint(resume_state.build_checkpoint(
                frontier=self._frontier.snapshot_items(),
                visited=self._visited_fingerprints,
                states=self._tracker.states,
                actions=self._tracker.actions,
                spent_keys=self._frontier.spent_keys(),
                sequence_index=self._next_seq,
                # Gate 1 / T-JC-02 — so a resume inherits progression the way it
                # already inherits states.
                journeys_walked=self._journeys_walked,
                journey_crossings=sum(
                    max(0, int(f.get("step_count") or 0) - 1)
                    for f in self._flows),
            ))
        except Exception:  # pragma: no cover - never fail a crawl for a checkpoint
            logger.warning("qec.crawler.checkpoint_failed crawl_id=%s", self.crawl_id,
                           exc_info=True)


class FinishMixin:
    """The terminal verdict, adjudicated from evidence."""

    # -- the terminal verdict (M1.7 / T-GW-01, T-GW-03) ------------------------

    def _finish(self, detail: str) -> CrawlSummary:
        """Adjudicate the terminal state from EVIDENCE and build the summary.

        THE LINE THIS REPLACES was ``if not self._stop_reason: self._stop_reason
        = STOP_COMPLETED`` - "nothing set a reason, therefore we finished". That
        is an inference from an absence, and it is the last link in every
        green-wash chain in the engine: a failed inventory read, an unrecoverable
        resume and a crawl that never reached a page all arrive there with an
        empty ``_stop_reason`` and all three used to claim ``completed``.

        The claim now goes through :func:`app.completion.adjudicate`, which may
        only ever pull a verdict DOWN: it can refuse ``completed`` for want of
        evidence, and it can never turn a failure into a success or invent a
        reason nobody set.

        Reached from BOTH terminal paths - the normal exit and the early resume
        refusal - so there is exactly one place a crawl can end. A second exit
        that skipped the adjudicator would be a green-wash hole in the code that
        closes green-wash holes.
        """
        claimed = self._stop_reason or STOP_COMPLETED
        verdict = completion.adjudicate(
            claimed,
            completion.CrawlEvidence(
                states=self._tracker.states,
                actions=self._tracker.actions,
                resumed_states=self._resume_plan.prior_states,
                inventory_failures=self._inventory_failures,
                resumed=self._resume_requested,
                resume_broken=(self._resume_requested
                               and not self._resume_plan.recoverable),
                # ── Gate 1 / T-JC-01 · DID ANY JOURNEY ACTUALLY MOVE ────────
                # Counted from the flow ledger this run built, which is the same
                # object the manifest's coverage summary is derived from — so an
                # auditor can recompute both numbers from the artifact on disk
                # without trusting the process that wrote them.
                #
                # A flow's ``step_count`` includes the step it STARTED on, so a
                # journey that arrived and never advanced counts 1 step and
                # contributes ZERO crossings. That subtraction is the whole
                # measurement: it is what separates "walked a funnel" from
                # "observed a funnel's first page N times".
                journeys_walked=self._journeys_walked,
                journey_crossings=sum(
                    max(0, int(f.get("step_count") or 0) - 1)
                    for f in self._flows),
                # ^ summed over ALL flows, including discovery's one-step ones:
                # they contribute exactly 0, so including them cannot inflate the
                # count, and excluding them would need a second bookkeeping path
                # that could drift from this one.
                # Inherited from the newest checkpoint of this crawl id
                # (T-JC-02). A resume that adds no new crossing because its
                # predecessor already walked the funnel HAS the evidence; it
                # simply did not add to it — the same doctrine `resumed_states`
                # applies to page states.
                #
                # A checkpoint is periodic, so this UNDER-counts by at most the
                # crossings made after the last one. That direction is safe by
                # construction: it can cause a conservative refusal and can never
                # manufacture a progression that did not happen.
                resumed_crossings=self._resume_plan.prior_crossings,
            ),
        )
        self._stop_reason = verdict.stop_reason
        self._done = True
        if verdict.downgraded:
            # LOUD, and in the manifest below. A refused completion claim is the
            # single most important thing an operator can be told about a crawl,
            # and the failure mode being fixed here is precisely one that used to
            # be silent.
            logger.error(
                "qec.crawler.completion_refused crawl_id=%s claimed=%s verdict=%s "
                "evidence=%s - %s",
                self.crawl_id, verdict.claimed_stop_reason, verdict.stop_reason,
                verdict.evidence, verdict.detail)
        detail = detail or verdict.detail or self._inventory_failure_detail
        self._emit_terminal_meta(detail)
        summary = CrawlSummary(
            crawl_id=self.crawl_id,
            stop_reason=self._stop_reason,
            states=self._tracker.states,
            actions=self._tracker.actions,
            screenshots=self._emitter.frame_count,
            guard_blocks=self._guard_blocks,
            manifest_path=str(emit.manifest_path(self.work_dir, self.crawl_id)),
            storage_state=self._storage_state,
            detail=detail,
            coverage=self._build_coverage(),
            max_depth_reached=self._max_depth_reached,
            disposition=verdict.disposition,
            evidence=dict(verdict.evidence),
            downgraded=verdict.downgraded,
            discovered_rules=self._rule_ledger.as_list(),
        )
        logger.info("qec.crawler.completed crawl_id=%s stop_reason=%s states=%d "
                    "actions=%d screenshots=%d guard_blocks=%d",
                    self.crawl_id, self._stop_reason, summary.states,
                    summary.actions, summary.screenshots, summary.guard_blocks)
        # TIER-3 LIVENESS, ONE GREPPABLE LINE (Track 3.1). An all-tier-1 crawl
        # and a crawl whose oracle was never wired produce identical advance
        # counts, so "is tier-3 alive" was an inference from an absence. It is
        # now a fact printed on every crawl whether or not the oracle was used.
        logger.warning(
            "qec.oracle.liveness crawl_id=%s advance_oracle=%s consults=%d "
            "picks=%d unavailable=%d errors=%d",
            self.crawl_id,
            "configured" if self._advance_oracle is not None else "none",
            self._oracle_consults, self._oracle_picks,
            self._oracle_unavailable, self._oracle_errors)
        return summary

    # -- AUTH phase ------------------------------------------------------------











    # -- EXPLORE phase ---------------------------------------------------------

                # one bad state must not kill the crawl — continue honestly.






    # -- href-follow traversal (SPA-robust link following) ---------------------
