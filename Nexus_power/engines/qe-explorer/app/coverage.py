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

from . import endpoint_inventory
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
    #: M3.1 / T-VIS-01 - every vision escalation, VERIFIED and REFUSED alike.
    #: Declared here for the same reason as the fields above: a payload key with
    #: no contract entry is how the crawler and its ledger drift apart.
    _vision_ledger: list[dict[str, Any]]
    #: M3.1 / T-VIS-03 - vision's own gate, cap, timeout and breaker.
    _vision_budget: Any
    _unhandled_controls: list[dict[str, str]]
    _submit_candidates: list[str]
    _approvable_boundary: list[dict[str, Any]]
    _outcome_milestones: list[dict[str, Any]]
    _crossings: Any
    _advance_blocked: list[dict[str, Any]]
    #: M2.5 - the network-evidence accumulators the account renders. Declared
    #: here for the same reason the M1.7 fields above are: a field that appears
    #: in the payload but not in this contract is how the crawler and its
    #: ledger drift apart without anything going red.
    _endpoint_inventories: list[dict[str, Any]]
    _network_server_errors: list[dict[str, str]]
    _network_events_seen: int
    #: M1.7 - the durable-learning and evidence-health attributes the coverage
    #: payload reads. Declared here (rather than duck-typed at the read) because
    #: this Protocol is the written contract between the crawler and its ledger,
    #: and a field that appears in the payload but not the contract is how the
    #: two drift apart.
    _rule_ledger: Any
    _known_rules: Any
    _inventory_failures: int
    _inventory_failure_detail: str
    _flows: list[dict[str, Any]]
    _forms_found: int
    _expansions_opened: int
    _expansions_skipped: int
    _tab_views_recorded: int
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

        def _dedup_boundary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Deduped by LOGICAL boundary, not by label.

            The same "Submit" button on two different pages is two boundaries
            and needs two approvals; deduping on the label alone would show the
            operator one row and silently authorise both.
            """
            seen: set[str] = set()
            out: list[dict[str, Any]] = []
            for d in items or ():
                key = str(d.get("boundary_key") or "")
                label = str(d.get("label") or "").strip()
                if not label or (key and key in seen):
                    continue
                if key:
                    seen.add(key)
                out.append({"label": label,
                            "url": str(d.get("url") or ""),
                            "reason": str(d.get("reason") or ""),
                            "rule_id": str(d.get("rule_id") or ""),
                            "severity": str(d.get("severity") or ""),
                            "boundary_key": key})
            return out[:80]

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
        # ── A4.3 / T-AC-01 — TWO LISTS, TWO MEANINGS ────────────────────────
        # `submit_candidates` used to hold both the controls the walk crosses
        # freely AND the irreversible ones it stops at, and qe-central built the
        # operator's approval picker from it. Dangerous controls were filtered
        # out of it at both producers, so the picker was built from a list that
        # structurally could not contain anything needing approval.
        approvable = _dedup_boundary(c._approvable_boundary)
        milestones = list(c._outcome_milestones or [])
        crossings = (c._crossings.to_list()
                     if getattr(c, "_crossings", None) is not None else [])
        # ── T-AC-06 — NOT `- c._forms_submitted` ────────────────────────────
        # That counter rises on every submit ATTEMPT, error or not, so an
        # application whose submits all fail reported its boundaries as
        # "exercised" and its funnel as worked. The ledger knows which boundaries
        # were actually crossed, by logical identity, so this subtracts a fact
        # instead of a tally.
        crossed_keys = {r.get("boundary_key") for r in crossings
                        if r.get("status") == "crossed"}
        unexercised = max(0, len([a for a in approvable
                                  if a.get("boundary_key") not in crossed_keys]))
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
        # ── M2.5 / T-NET-04 — THE APPLICATION'S API SURFACE ─────────────────
        # Folded from the per-visit inventories rather than rebuilt from a
        # concatenated event stream, so a long crawl never has to hold every
        # event it ever saw in memory. The merge is associative on the same key,
        # so the result is the object a single build over the whole stream would
        # have produced.
        #
        # This is NOT `states[*].endpoints` and must not be confused with it.
        # That map is deliberately narrow — 2xx only, path only — because it
        # feeds the compiler, and compiling a 5xx into an assertion would freeze
        # the application's bug into the regression suite as expected behaviour.
        # THIS is the inventory: every observed status including the failures,
        # every retry counted, the auth pattern, the response shape, and the UI
        # action each endpoint was seen to fire behind.
        inventory = endpoint_inventory.merge_inventories(c._endpoint_inventories)
        server_errors = list(c._network_server_errors or [])
        return {
            "forms_found": c._forms_found,
            # M2.6 / T-CAP-03 - collapsed sections this crawl deliberately
            # opened before cataloguing, and disclosures it declined to
            # merge (a tab strip whose panels are never on screen together).
            "expansions_opened": c._expansions_opened,
            "expansions_skipped": c._expansions_skipped,
            "tab_views_recorded": c._tab_views_recorded,
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
            # ── M3.1 · THE VISION EVIDENCE (T-VIS-01 / T-VIS-03) ────────────
            # BOTH halves, and the spend that produced them.
            #
            # `vision_verified` is the only number that may be read as coverage:
            # it counts controls that were clicked at a perceived coordinate and
            # then MEASURED responding. `vision_refused` counts the model's
            # unproven guesses, and it is published precisely because a wrong
            # perception that leaves no trace is indistinguishable from a
            # perception that never happened.
            #
            # `vision_budget` carries the gate (attested? tenant-enabled? under
            # which attestation rung?), the cap, the breaker and every refusal
            # reason — so "this crawl made no vision calls" always has a stated
            # cause rather than being an absence a reader has to interpret.
            "vision_ledger": list(getattr(c, "_vision_ledger", []) or [])[:200],
            "vision_verified": sum(
                int(r.get("verified") or 0)
                for r in (getattr(c, "_vision_ledger", []) or [])),
            "vision_refused": sum(
                int(r.get("refused") or 0)
                for r in (getattr(c, "_vision_ledger", []) or [])),
            "vision_budget": (c._vision_budget.telemetry()
                              if getattr(c, "_vision_budget", None) is not None
                              else {}),
            # Interactive controls the matcher has no primitive for → the ledger's UNHANDLED rows.
            "unhandled_controls": unhandled_controls,
            # Controls the walk may cross on its own authority. NOT an approval
            # list — see approvable_boundary for that.
            "submit_candidates": submits,
            # ── THE APPROVAL SURFACE (A4.3 / T-AC-01) ───────────────────────
            # Every irreversible control this crawl MET and did not cross, with
            # the reason it is one. This is what an operator picks a
            # `boundary_approvals` grant from — and until it existed there was
            # nothing to pick from, which is why no journey had ever completed.
            "approvable_boundary": approvable,
            # ── THE CROSSINGS AND THEIR LANDINGS (T-AC-03 / T-AC-04) ────────
            # Every attempt, including refusals: a crawl that reached the commit
            # button and was not authorised must be distinguishable from one
            # that never got there.
            "boundary_crossings": crossings,
            # The verified landings. `verified` on each is DERIVED from the
            # observed transition; nothing may set it directly.
            "outcome_milestones": milestones,
            # THE PRODUCT CLAIM, computed from the milestones alone.
            "journeys_completed": sum(1 for m in milestones if m.get("verified")),
            "boundaries_crossed": sum(1 for r in crossings
                                      if r.get("status") == "crossed"),
            # Why a funnel stopped one step in, named. A walk that declines is
            # honest but silent; this is the sentence that makes it actionable.
            "advance_blocked": c._advance_blocked[:40],
            # ── M1.7 / T-GW-04 · DURABLE LEARNING ───────────────────────────
            # The rules THIS crawl proved, keyed and versioned, for qe-central to
            # persist against (tenant, app). Until this existed the proof lived
            # only as a sentence inside ``advance_blocked`` - readable by a human,
            # indexable by nothing - so every crawl of the same application
            # re-ran the same experiment to re-derive it.
            "discovered_rules": c._rule_ledger.as_list(),
            # Whether inherited knowledge was actually USED, and how often. The
            # reuse RATE is a headline metric of this milestone, and a metric
            # derived after the fact from log lines is a metric nobody can hold a
            # gate on.
            "rule_reuse": c._known_rules.stats(),
            # ── M1.7 / T-GW-01 · READS THAT FAILED ──────────────────────────
            # Pages the crawl could NOT observe. Non-zero here is why a crawl
            # reports ``inventory_failed`` instead of ``completed``; it is carried
            # into coverage so the refusal is explainable from the artefact and
            # not only from the container logs.
            "inventory_failures": c._inventory_failures,
            "inventory_failure_detail": c._inventory_failure_detail[:500],
            # THE QUESTIONS EACH STATE ASKED — the producer side of a contract
            # qe-central has always read and nothing has ever written. See
            # _note_state_signals: without this the Master Catalog can only ever
            # hold the questions that arrive as journey BRANCHES (choices), and
            # every text, date and number field in every application is missing
            # from the catalogue the client is shown.
            "states": c._state_signals(),
            # ── M2.5 — NETWORK EVIDENCE, AS AN ACCOUNT RATHER THAN A LOG ────
            # `endpoint_inventory` is the application-level API surface
            # (method x path template). No raw URL, no header value and no body
            # value reaches it: the raw per-visit stream lives on the
            # `page_state` records, where its blast radius is one crawl, and the
            # catalog gets the aggregate.
            "endpoint_inventory": inventory.get("endpoints", []),
            "endpoint_inventory_truncated": bool(inventory.get("truncated")),
            "network_events_observed": int(c._network_events_seen),
            # Every OBSERVED 5xx, read as an integer from the structured stream.
            # This is the network oracle's input and the reason it no longer has
            # to search arbitrary error strings to learn the backend failed —
            # each row names the request AND the UI action that caused it.
            "network_server_errors": server_errors,
            "network_server_error_count": len(server_errors),
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
                f"{unexercised} irreversible boundary/ies awaiting approval; "
                f"{sum(1 for m in milestones if m.get('verified'))} journey(s) "
                f"completed end-to-end through an approved crossing."
            ),
        }


__all__ = ["CoverageHost", "CoverageLedger"]
