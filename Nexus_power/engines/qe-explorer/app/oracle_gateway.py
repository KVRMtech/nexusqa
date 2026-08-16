"""The single seam through which crawler internals consult an LLM (T-DE-09).

Before this module, ``Crawler`` held two raw callables and five loose counters,
and every consultation site independently owned "is it configured? did it
raise? how long did it take? was the reply readable?".  Behind the gateway
there is exactly one place to look, and the telemetry has one home instead of
five.

WHY THE TELEMETRY IS NOT DECORATION.  Tier 3 is the mechanism that makes "works
on any label convention" true, and it had never been observed working: a crawl
whose advances were all tier-1 is indistinguishable from a crawl whose oracle
was dead.  An outage has to surface as a NUMBER, not as mysteriously
one-step journeys.  So ``consults`` / ``errors`` / ``unavailable`` /
``latency_ms`` / ``picks`` are behaviour, and they are moved here unchanged.

THE CAPS DO NOT LIVE HERE.  The per-crawl circuit breaker and call cap are
inside the callables built by :mod:`app.main`.  This gateway WRAPS; it does not
re-implement.  ``unavailable`` stays a first-class, non-raising outcome — a
missing or failing oracle degrades the crawl honestly, it never fails it.

SCOPE NOTE — ``operate`` (the medic oracle).  The frozen
:class:`app.protocols.OracleGateway` declares three verbs, but the medic oracle
has no crawler-side consumer: ``app.main`` passes it straight into
``PlaywrightBrowserPort(medic_oracle=...)``, where the interaction ladder uses
it.  It is already encapsulated one layer BELOW the crawler.  Routing it
through here would add a hop that no caller wants and would move a
browser-layer concern up into the orchestrator, so ``operate`` is deliberately
not implemented — see :meth:`OracleGateway.operate`.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)


class OracleGateway:
    """Wraps the advance + vision oracles with telemetry and memoization."""

    def __init__(self, advance_oracle: Optional[Any], vision_oracle: Optional[Any],
                 clock: Any) -> None:
        self._advance = advance_oracle
        self._vision = vision_oracle
        self._clock = clock
        self.consults = 0
        self.errors = 0
        self.unavailable = 0
        self.latency_ms = 0
        self.picks = 0
        #: Tier-3 outcomes memoized per state fingerprint: the wizard entry check
        #: and the loop's first iteration see the SAME page, and a re-visited step
        #: must not pay a second LLM call. ``unavailable`` is deliberately NOT
        #: memoized — transient trouble may pass; the circuit breaker (in the
        #: oracle callable) owns systemic failure. Value: (picked control name or
        #: None, oracle status, decision-point signature).
        self.memo: dict[str, tuple[Optional[str], str, str]] = {}

    # -- configuration ---------------------------------------------------------

    @property
    def advance_configured(self) -> bool:
        return self._advance is not None

    @property
    def vision_configured(self) -> bool:
        return self._vision is not None

    @property
    def telemetry(self) -> dict[str, int]:
        return {"consults": self.consults, "picks": self.picks,
                "unavailable": self.unavailable, "errors": self.errors,
                "latency_ms": self.latency_ms}

    # -- the verbs -------------------------------------------------------------

    async def advance(
        self, controls: Sequence[dict[str, Any]], page_title: str, page_url: str,
    ) -> Optional[dict[str, Any]]:
        """Ask which control moves this funnel forward.

        Returns the oracle's raw reply, or ``None`` when it raised.  The caller
        maps the reply onto its own decision type; shaping that decision is a
        traversal concern, not an oracle one.  Never raises.
        """
        self.consults += 1
        started = self._clock.now_ms()
        try:
            outcome = await self._advance(controls, page_title, page_url)
        except Exception as exc:
            outcome = None
            self.errors += 1
            logger.warning("qec.oracle.consult_failed url=%s err=%s",
                           (page_url or "")[:120], exc)
        self.latency_ms += max(0, self._clock.now_ms() - started)
        return outcome

    async def perceive(self, screenshot_b64: str,
                       page_context: Mapping[str, Any]) -> dict[str, Any]:
        """The DOM is opaque — enumerate what is visibly on screen.

        Empty for a tenant without the vision flag (the server side enforces
        that), and empty for an unreadable reply.

        DELIBERATELY PROPAGATES.  Unlike :meth:`advance`, this does NOT swallow
        exceptions, because the single call site already wraps the whole
        screenshot-and-perceive sequence in ``except Exception: pass``.  Adding
        a catch here would change observable behaviour in two ways: it would
        emit a log line on a path that is currently silent, and it would let
        execution continue past a failure the caller intends to abandon.  A
        gateway that "improves" an error path is still changing behaviour.
        """
        if self._vision is None:
            return {}
        return await self._vision(screenshot_b64, dict(page_context)) or {}

    async def operate(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """NOT ROUTED THROUGH THE CRAWLER — see the module docstring.

        The medic oracle is handed to :class:`app.playwright_port.PlaywrightBrowserPort`
        by :mod:`app.main` and consumed by the interaction ladder inside the
        browser layer.  Declared here so the frozen protocol's third verb has an
        honest answer rather than a silent omission.
        """
        raise NotImplementedError(
            "the medic oracle is owned by PlaywrightBrowserPort, not the crawler")

    # -- telemetry the caller records ------------------------------------------

    def note_unavailable(self) -> None:
        self.unavailable += 1

    def note_pick(self) -> None:
        self.picks += 1


__all__ = ["OracleGateway"]
