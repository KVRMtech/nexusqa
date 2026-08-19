"""Crawl-level manifest metadata and network draining (M0.3 / T-DE-07).

Extracted VERBATIM from :mod:`app.crawler`.

TWO ``crawl_meta`` RECORDS BRACKET EVERY CRAWL, and the pair is a contract:
the opening record states the configuration the crawl was launched under, and
the closing record states how it ended plus the counters it actually reached.
A manifest with only the first is a crash; a reader can tell the difference
without guessing.  ``stop_reason`` on the terminal record is the crawl's honest
verdict about itself, which is why it is never defaulted or inferred here — it
is whatever the state machine set.

``drain_network`` lives here because what it produces is manifest evidence:
the XHR/fetch calls an application made during a visit.  It is best-effort and
non-raising by contract — a browser adapter that cannot report network traffic
degrades the evidence, it never fails the crawl.

Like the other extracted collaborators, this module declares the interface it
needs (:class:`EmitterHost`) and never imports :mod:`app.crawler`.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, Sequence

logger = logging.getLogger(__name__)


class EmitterHost(Protocol):
    """The slice of crawl state the metadata emitter reads."""

    crawl_id: str
    target_url: str
    _port: Any
    _emitter: Any
    _tracker: Any
    _guard: Any
    _budget: Any
    _explorer_version: str
    _guard_version: str
    _refuse_pack_version: str
    _config_fingerprint: str
    _stop_reason: str
    _guard_blocks: int
    _scope_path_prefixes: tuple[str, ...]


class MetaEmitter:
    """Writes the crawl's opening and terminal ``crawl_meta`` records."""

    def __init__(self, host: EmitterHost, attestation_dict: Any) -> None:
        self._c = host
        #: Injected rather than imported: the attestation projector lives in the
        #: crawler's module scope, and importing it back would rebuild the very
        #: cycle this decomposition exists to remove.
        self._attestation_dict = attestation_dict

    async def drain_network(self) -> list[dict[str, Any]]:
        """API/network mining — drain the XHR/fetch calls the app made during this
        visit from the port's capture buffer (an optional verb, accessed by
        ``getattr`` so a fake/older adapter without it is a clean no-op).  The
        adapter buffers + PII-scrubs at source; here we only relay best-effort."""
        drain = getattr(self._c._port, "drain_network", None)
        if drain is None:
            return []
        try:
            return list(await drain() or [])
        except Exception:
            logger.warning("qec.crawler.network_drain_failed", exc_info=True)
            return []

    async def drain_browser_events(self) -> int:
        """M1.5 — drain the port's special-browser-event buffer and EMIT each
        entry to the manifest.  Returns how many were written.

        Unlike ``drain_network``, which hands its result back to the caller to
        fold into a ``page_state``, these are emitted here and now.  A popup
        adoption, a dialog answer and a captured download are facts about the
        SESSION, not about one page's inventory — and emitting at the drain
        keeps them in the manifest even when the visit that produced them ends
        without recording a state (a de-duplicated fingerprint, an out-of-scope
        landing, a budget stop).  Evidence that only survives the happy path is
        the kind that is missing exactly when it is needed.

        Optional + best-effort, exactly like its sibling: a fake or older
        adapter with no such buffer is a clean no-op.
        """
        drain = getattr(self._c._port, "drain_browser_events", None)
        if drain is None:
            return 0
        try:
            events = list(await drain() or [])
        except Exception:
            logger.warning("qec.crawler.browser_event_drain_failed", exc_info=True)
            return 0
        written = 0
        for event in events:
            if not isinstance(event, dict):
                continue
            try:
                self._c._emitter.emit_browser_event(event)
                written += 1
            except Exception:
                logger.warning("qec.crawler.browser_event_emit_failed", exc_info=True)
        return written

    def emit_initial_meta(self) -> None:
        self._c._emitter.emit_crawl_meta(self.meta(stop_reason=""))

    def emit_terminal_meta(self, detail: str) -> None:
        c = self._c
        meta = self.meta(stop_reason=c._stop_reason)
        meta["frame_count"] = c._emitter.frame_count
        meta["stats"] = c._tracker.snapshot()
        meta["guard_blocks"] = c._guard_blocks
        if detail:
            meta["detail"] = detail
        c._emitter.emit_crawl_meta(meta)

    def meta(self, *, stop_reason: str) -> dict[str, Any]:
        c = self._c
        attestation = c._guard.attestation
        meta = {
            "crawl_id": c.crawl_id,
            "target_url": c.target_url,
            "explorer_version": c._explorer_version,
            "config_fingerprint": c._config_fingerprint,
            "frame_count": c._emitter.frame_count,
            "budgets": c._budget.as_dict(),
            "guard_version": c._guard_version,
            "refuse_pack_version": c._refuse_pack_version,
            "attestation": self._attestation_dict(attestation),
            # M1.3 — whether this crawl held a verified platform provisioning
            # proof, and what it spent. Recorded on EVERY crawl, granted or not:
            # a walk that stopped at a Save Draft has to be able to say why, and
            # "no walk persistence was authorised" is the answer.
            "walk_persistence": self._walk_persistence_dict(),
            "stop_reason": stop_reason,
        }
        if c._scope_path_prefixes:  # Target-mode audit trail (mapper ignores extras)
            meta["scope_path_prefixes"] = list(c._scope_path_prefixes)
        return meta


    def _walk_persistence_dict(self) -> dict[str, Any]:
        auth = getattr(self._c._guard, "walk_authorization", None)
        if auth is None:
            from .walk_persist import unauthorized_summary
            return unauthorized_summary(
                getattr(self._c, "_walk_denied_reason", "") or "not_attested")
        return auth.summary()


__all__ = ["EmitterHost", "MetaEmitter"]
