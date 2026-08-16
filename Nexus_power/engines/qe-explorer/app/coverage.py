"""The crawl's coverage account (M0.3 / T-DE-07).

Extracted VERBATIM from :mod:`app.crawler`.  Coverage is the crawl's honest
self-report: what was FOUND versus what could actually be filled, advanced and
submitted — and, when authentication did not fully succeed, WHICH remediation
the operator should perform.

WHY THAT LAST PART IS LOAD-BEARING.  Four different auth failures need four
different instructions, and three of them are actively harmful if confused:
telling an operator to "re-record the login" when signing in demonstrably works
sends them after a proven-correct artefact, and telling them to "re-crawl" when
the entry is gated and no credentials exist is an instruction that cannot ever
succeed.  The prose branches are therefore behaviour, not decoration, and they
are moved here character-for-character.

The module owns no state.  It reads the crawl through :class:`CoverageHost`,
an interface it declares itself, so it never imports :mod:`app.crawler`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional, Protocol, Sequence

from . import flow_ledger
from .auth import (AUTH_NO_CREDENTIALS, AUTH_NOT_PERSISTED,
                   AUTH_SESSION_EXPIRED)


class CoverageHost(Protocol):
    """The slice of crawl state the coverage account reads.

    Declared by the consumer, satisfied by ``Crawler`` — the dependency points
    at this interface, not at the god object.
    """

    _field_ledger: list[dict[str, Any]]
    _filled_by_kind: dict[str, int]
    _fields_inferred: list[str]
    _fields_unfilled: list[str]
    _fields_seed_detail: list[dict[str, str]]
    _opaque_surfaces: list[dict[str, str]]
    _unhandled_controls: list[dict[str, str]]
    _submit_candidates: list[str]
    _advance_blocked: list[dict[str, Any]]
    _flows: list[dict[str, Any]]
    _forms_found: int
    _forms_submitted: int
    _forms_confirmed: int
    _open_choice_unverified: int
    _crawl_mode: str
    _traversal: str
    _advance_oracle: Optional[Any]
    _oracle_consults: int
    _oracle_picks: int
    _oracle_unavailable: int
    _oracle_errors: int
    _oracle_latency_ms: int
    _auth_incomplete: bool
    _auth_incomplete_reason: str
    _auth_blocked_reason: str
    _credentials: Optional[Any]

    def _state_signals(self) -> list[dict[str, Any]]: ...


class CoverageLedger:
    """Accumulates per-page findings and renders the crawl-wide account."""

    def __init__(self, host: CoverageHost) -> None:
        self._c = host

    # -- accumulation ---------------------------------------------------------

    def collect_ledger(self, entries: list[dict[str, Any]], url: str) -> None:
        """Merge this state's field ledger into the crawl-wide one, deduped by
        signature.

        Deduped because the same field on ten pages is ONE thing to ask the client
        about — a residue list that repeats itself is the reason operators stop
        reading it. The first sighting wins and keeps its page, which is what lets
        the ask say WHICH flow the field belongs to."""
        ledger = self._c._field_ledger
        seen = {e.get("signature") for e in ledger}
        for entry in entries or ():
            sig = entry.get("signature")
            if not sig or sig in seen:
                continue
            seen.add(sig)
            row = dict(entry)
            row["url"] = url
            ledger.append(row)

    def note_fills_by_kind(self, counts: Mapping[str, int]) -> None:
        """Roll one page's committed fills into the crawl-wide count.

        ``auto_filled`` cannot distinguish five answered DROPDOWNS from five
        answered text boxes, and the dropdown is the widget class that keeps
        breaking — a portal-rendered choice reads back empty, the fill is
        discarded, and the total barely moves because text fields carried it.
        The one number a gate most needs to hold was the one it could not see.
        """
        by_kind = self._c._filled_by_kind
        for kind, n in (counts or {}).items():
            k = str(kind)
            by_kind[k] = by_kind.get(k, 0) + int(n)

    # -- the account ----------------------------------------------------------

    def build(self) -> dict[str, Any]:
        """The crawl's coverage account (deduped, first-appearance order): what was
        found vs could be filled/advanced. ``forms_submitted`` is 0 in the explore
        phase (the submit boundary) — ``submit_candidates`` are the flows a Phase-B
        attested submit would carry deeper. Turns the shallow-vs-full gap into a
        NAMED, targeted seed request instead of blind guessing."""
        c = self._c

        def _dedup(items: list[str]) -> list[str]:
            seen: set[str] = set()
            out: list[str] = []
            for it in items:
                k = (it or "").strip()
                if k and k.lower() not in seen:
                    seen.add(k.lower())
                    out.append(k)
            return out

        def _dedup_detail(items: list[dict[str, str]]) -> list[dict[str, str]]:
            seen: set[str] = set()
            out: list[dict[str, str]] = []
            for d in items:
                lbl = (d.get("label") or "").strip()
                if lbl and lbl.lower() not in seen:
                    seen.add(lbl.lower())
                    out.append({"label": lbl, "url": (d.get("url") or "").strip()})
            return out

        def _dedup_opaque(items: list[dict[str, str]]) -> list[dict[str, str]]:
            seen: set[str] = set()
            out: list[dict[str, str]] = []
            for d in items:
                k = f"{d.get('kind')}|{d.get('label')}"
                if k not in seen:
                    seen.add(k)
                    out.append({"kind": str(d.get("kind") or ""),
                                "label": str(d.get("label") or ""),
                                "reason": str(d.get("reason") or "")})
            return out

        def _dedup_unhandled(items: list[dict[str, str]]) -> list[dict[str, str]]:
            seen: set[str] = set()
            out: list[dict[str, str]] = []
            for d in items:
                lbl = str(d.get("label") or "").strip()
                if lbl and lbl.lower() not in seen:
                    seen.add(lbl.lower())
                    out.append({"label": lbl, "kind": str(d.get("kind") or ""),
                                "reason": str(d.get("reason") or "")})
            return out

        inferred = _dedup(c._fields_inferred)
        needs_seed = _dedup(c._fields_unfilled)
        field_ledger = c._field_ledger[:500]
        needs_seed_detail = _dedup_detail(c._fields_seed_detail)
        opaque_surfaces = _dedup_opaque(c._opaque_surfaces)
        unhandled_controls = _dedup_unhandled(c._unhandled_controls)
        submits = _dedup(c._submit_candidates)
        unexercised = max(0, len(submits) - c._forms_submitted)
        # Honest, LOUD auth prefix: if credentials were supplied but no login form could
        # be driven, the crawl covered PUBLIC pages only — say so plainly, never imply the
        # authenticated app was covered.
        if c._auth_blocked_reason == AUTH_NO_CREDENTIALS:
            # Gated entry + NO credentials/session supplied → the crawl stopped at the
            # sign-in without exploring. Name the correct remediation (record a login /
            # attach credentials); never the credentials-supplied wording below.
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — the entry is behind a login wall and "
                "NO credentials or session were supplied; the crawl stopped at the "
                "sign-in without exploring. Record a login or attach a member card, "
                "then re-crawl. "
            )
        elif not c._auth_incomplete:
            auth_prefix = ""
        elif c._auth_incomplete_reason == AUTH_NOT_PERSISTED:
            # Signing in WORKS; the app just will not keep it. Naming re-recording or
            # new credentials here would send the operator after two things that are
            # already proven correct.
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — the crawl SIGNED IN successfully, but "
                "this app drops the sign-in on every page load (it keeps the signed-in "
                "user in the page, not in a cookie), so pages behind the login could not "
                "be reached. Re-recording and new credentials will not change this. "
            )
        elif c._auth_incomplete_reason == AUTH_SESSION_EXPIRED and not c._credentials:
            # A session was injected and REJECTED, and there is no username/password to
            # fall back on. "Re-record" is a loop that cannot end here: the next
            # recording captures another session, and an app whose login lives in
            # client-side state can never restore one. Name the durable fix instead.
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — the app rejected the stored login "
                "session and NO username/password is configured, so the crawl could not "
                "sign in; crawled the accessible (public) pages only. Add a username and "
                "password so the crawl signs itself in — recording again captures "
                "another session that can fail the same way. "
            )
        elif c._auth_incomplete_reason == AUTH_SESSION_EXPIRED:
            # Different cause, different remediation: nobody needs to check the
            # credentials — the stored SESSION died and must be re-recorded.
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — the stored login session has "
                "EXPIRED (the app still presented a login wall while holding it); "
                "crawled the accessible (public) pages only. Re-record the login to "
                "restore authenticated coverage. "
            )
        else:
            auth_prefix = (
                "AUTHENTICATED AREAS NOT COVERED — credentials were supplied but no login "
                f"form was found/completed at the entry ({c._auth_incomplete_reason}); "
                "crawled the accessible (public) pages only. "
            )
        return {
            "forms_found": c._forms_found,
            "forms_submitted": c._forms_submitted,
            # Of those, the ones the APP confirmed. Ratcheted separately: a
            # floor on attempts cannot tell a working funnel from a broken one.
            "forms_confirmed": c._forms_confirmed,
            "fields_inferred": inferred,
            # PER-FIELD LEDGER (field learning). One entry per distinct field the
            # crawl met: what it is, how it was answered, whether it committed.
            # No values — this travels back to qe-central and into evidence.
            "field_ledger": field_ledger,
            # BUSINESS FLOWS. Each journey walked, and whether it REACHED THE END.
            # The summary states branch_coverage=False explicitly, so walking every
            # flow once can never be read as having covered every business path.
            "flows": c._flows[:200],
            "flow_summary": flow_ledger.summarize(c._flows),
            "crawl_mode": c._crawl_mode,
            # TRAVERSAL POSTURE this crawl actually ran under. Recorded because it
            # decides whether a funnel was walked to its end or SAMPLED: "6 steps,
            # terminal=budget" means something entirely different under `probe` than
            # under `full`, and a reader who cannot tell them apart will read a
            # sample as coverage.
            "traversal": c._traversal,
            "fields_needing_seed": needs_seed,
            # Per-field page context {label, url} — the grounded source for flow grouping.
            # Kept alongside the flat list (which stays for back-compat).
            "fields_needing_seed_detail": needs_seed_detail,
            # DOM-unreadable surfaces detected on the crawl → the ledger's OPAQUE rows.
            "opaque_surfaces": opaque_surfaces,
            # Interactive controls the matcher has no primitive for → the ledger's UNHANDLED rows.
            "unhandled_controls": unhandled_controls,
            "submit_candidates": submits,
            # Why a funnel stopped one step in, named. A walk that declines is
            # honest but silent; this is the sentence that makes it actionable.
            "advance_blocked": c._advance_blocked[:40],
            # THE QUESTIONS EACH STATE ASKED — the producer side of a contract
            # qe-central has always read and nothing has ever written. See
            # _note_state_signals: without this the Master Catalog can only ever
            # hold the questions that arrive as journey BRANCHES (choices), and
            # every text, date and number field in every application is missing
            # from the catalogue the client is shown.
            "states": c._state_signals(),
            # A choice widget that would not confirm its own answer. Was a log
            # line only, so the fix that took it from 6 to 0 could regress with
            # nothing to notice — now a number the gate holds at zero.
            "open_choice_unverified": c._open_choice_unverified,
            # Committed fills per control kind — lets a gate hold "the five
            # dropdowns were answered" instead of only "29 fields were".
            "filled_by_kind": dict(c._filled_by_kind),
            # TIER-3 LIVENESS + TELEMETRY (Track 3.1/3.3). `configured` says the
            # mechanism was WIRED; the counts say whether it was ever asked and
            # what it answered. Without this, "is tier-3 alive" is an inference
            # from an absence — the question the all-tier-1 advance counts left
            # permanently open.
            "advance_oracle": {
                "state": "configured" if c._advance_oracle is not None else "none",
                "consults": c._oracle_consults,
                "picks": c._oracle_picks,
                "unavailable": c._oracle_unavailable,
                "errors": c._oracle_errors,
                "latency_ms_total": c._oracle_latency_ms,
            },
            # Auth was requested but no login form could be driven → PUBLIC-only crawl.
            # Surfaced as first-class coverage so the operator is never misled.
            "auth_incomplete": c._auth_incomplete,
            "auth_reason": c._auth_incomplete_reason,
            # Entry gated + NO credentials/session supplied → the crawl STOPPED at the
            # login wall (STOP_AUTH_REQUIRED). Distinct from auth_incomplete (partial,
            # unauthenticated coverage): this is a hard block the app UI reports as
            # "behind a login, no credentials" so re-crawling alone is never advised.
            "auth_blocked": bool(c._auth_blocked_reason),
            "auth_blocked_reason": c._auth_blocked_reason,
            "summary": (
                auth_prefix
                + f"{c._forms_found} form(s) found; "
                f"{len(inferred)} field(s) auto-filled with a default; "
                f"{len(needs_seed)} field(s) need a real seed; "
                f"{c._forms_submitted} submit(s) exercised (Phase-B), "
                f"{unexercised} at the submit boundary."
            ),
        }


__all__ = ["CoverageHost", "CoverageLedger"]
