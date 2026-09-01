"""The crawler's thin forwarding to its subsystems.

Extracted from ``crawler.py`` (Gate 0 · task 12), which stood at 1294 LOC
against a <900 exit target. A PURE RELOCATION: not one character of logic,
ordering or naming changed, so the characterization goldens are byte-identical
by construction rather than by re-baselining.

Every method here is a one- or two-line delegation to ``_filler``,
``_recorder`` or ``_meta_emitter``. They are the SEAM between the driver and
its subsystems, which is exactly the boundary the mixin split exists to make
visible — a seam is easier to review when it is not interleaved with the loop
that uses it.
"""
from __future__ import annotations

import logging
from typing import Any, Sequence
from urllib.parse import urlsplit

from .browser import PageObservation
from .guard import registrable_domain, same_registrable_domain

logger = logging.getLogger(__name__)


class DelegatesMixin:
    """Forwarding to _filler / _recorder / _meta_emitter."""

    async def _probe_select_options(
        self, controls: Sequence[dict[str, Any]], *, url: str,
    ) -> None:
        await self._filler.probe_select_options(controls, url=url)

    def _set_options(self, control: dict[str, Any], options: Sequence[str]) -> None:
        self._filler.set_options(control, options)

    async def _commit_act(self, control: dict[str, Any]) -> bool:
        return await self._filler.commit_act(control)

    async def _commit_choice(self, control: dict[str, Any]) -> bool:
        return await self._filler.commit_choice(control)

    async def _probe_dependencies(
        self, controls: list[dict[str, Any]], *, url: str,
    ) -> None:
        await self._filler.probe_dependencies(controls, url=url)

    def _in_scope_key(self, url: str) -> str:
        """Path-level identity of a URL for the ACT-THEN-DIFF nav guard (host+path, query/
        hash-insensitive) — a same-page DOM change must NOT read as a navigation."""
        parts = urlsplit(url or "")
        return f"{(parts.hostname or '').lower()}{parts.path}"





    # -- wizard / stepper traversal (#1) ---------------------------------------











    # -- state recording -------------------------------------------------------

    def _record_state(self, **kwargs: Any) -> None:
        """Assemble + emit ONE ``page_state`` record with monotonic indices."""
        self._recorder.record_state(**kwargs)

    # -- helpers ---------------------------------------------------------------

    async def _observe(self) -> PageObservation:
        return await self._recorder.observe()

    async def _drain_network(self) -> list[dict[str, Any]]:
        return await self._meta_emitter.drain_network()

    async def _drain_browser_events(self) -> int:
        """M1.5 — flush the port's popup / dialog / download / page-close
        evidence into the manifest.  Best-effort; never raises."""
        try:
            return await self._meta_emitter.drain_browser_events()
        except Exception:  # pragma: no cover — evidence, never a crawl-stopper
            logger.warning("qec.crawler.browser_event_flush_failed", exc_info=True)
            return 0

    def _journey_context(self) -> dict[str, Any]:
        """The live journey context the browser port resolves dialog intent
        against (M1.5 / T-ND-02).

        A CALLABLE handed to the port rather than a value copied into it: the
        guard phase changes several times per crawl (explore → auth → walk →
        submit) and a snapshot taken at construction would be stale by the first
        dialog.  The port never imports the crawler; the crawler pushes this
        reader in, which keeps the dependency arrow pointing the same way M0.3
        set it.
        """
        try:
            phase = self._guard.phase.value
        except Exception:
            phase = ""
        return {
            "phase": phase,
            "observe_only": self._observe_only,
            # A4.3 grants. A native confirm is not an approval, so the ONLY way
            # a destructive dialog is ever accepted is an operator grant that
            # already exists for that control.
            "approved_labels": tuple(sorted(self._submit_approvals)),
        }

    async def _politeness_delay(self) -> None:
        rate = self._budget.rate_per_s
        if rate and rate > 0:
            await self._sleep(1.0 / rate)

    def _in_scope(self, url: str) -> bool:
        parts = urlsplit(url or "")
        host = (parts.hostname or "").lower()
        if not host:
            return False
        host_ok = (
            (self._target_host and same_registrable_domain(host, self._target_host))
            or host in self._allowed_hosts
            or registrable_domain(host) in self._allowed_registrable
        )
        if not host_ok:
            return False
        # TARGET MODE: the crawl is confined to the supplied journey's path
        # prefixes — a URL on the right host but outside every prefix is out of
        # scope (the whole point of Mode 2: exhaustive validation of ONE
        # workflow, no unrelated exploration). Query/fragment never matter.
        if not self._scope_path_prefixes:
            return True
        path = parts.path or "/"
        for p in self._scope_path_prefixes:
            if p == "/" or path == p or path.startswith(p + "/"):
                return True
        return False

    def _emit_initial_meta(self) -> None:
        self._meta_emitter.emit_initial_meta()

    def _emit_terminal_meta(self, detail: str) -> None:
        self._meta_emitter.emit_terminal_meta(detail)

    def _meta(self, *, stop_reason: str) -> dict[str, Any]:
        return self._meta_emitter.meta(stop_reason=stop_reason)

# ─── module helpers ──────────────────────────────────────────────────────────
