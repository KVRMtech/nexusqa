"""E3 — Catalog service (surfacing the control inventory with provenance).

The crawl manifest already captures rich per-page-state data: form controls
with types/options/required, displayed outcome values with selectors, and a
field ledger with semantic types and signatures.  The journey graph stores
only coarse booleans per node (is_decision, is_boundary, has_outcome).

This service bridges the gap: it extracts the per-node control inventory
from the manifest's page states and field ledger during fold, and composes
catalog views at query time with honest provenance badges.

Provenance is computed at QUERY TIME, not stored:

  * **observed** — the crawl saw it (default for everything)
  * **confirmed** — the journey baseline has been approved (O0 lifecycle)
  * **client_declared** — a client-authored rule in the answer_key explicitly
    declares a field or expected value (O1 rule oracle)

Pure + dependency-free (unit-testable with plain dicts).  Tolerant: bad
inputs produce empty results, never crash.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

PROVENANCE_OBSERVED = "observed"
PROVENANCE_CONFIRMED = "confirmed"
PROVENANCE_CLIENT_DECLARED = "client_declared"

_CONFIRMED_STATUSES = frozenset({"approved", "validated"})

#: HTML constraint attributes that are pure validation shape (never user values).
_VALIDATION_KEYS = ("pattern", "minlength", "maxlength", "min", "max", "step")

#: M2.1 - whether a catalogued question's WORDING is the application's own.
#: ``observed``   - read from a declared accessible-name rung in the page.
#: ``unverified`` - the application states no wording for it, and this catalogue
#: refuses to invent one. The question is still identified and still answerable;
#: only its text is missing, and it is missing because it was never there.
QUESTION_NAME_OBSERVED = "observed"
QUESTION_NAME_UNVERIFIED = "unverified"

#: WHERE A QUESTION'S DEPENDENCY CAME FROM.
#: ``declared``      - the page itself stated it (``depends_on`` in the form
#:                     signal). That is the application's own CLAIM about its
#:                     form, and it is carried as such.
#: ``proven_reveal`` - no page declared anything; a crawl ANSWERED the trigger
#:                     and the child question appeared (ACT-THEN-DIFF). That is
#:                     evidence rather than a claim, and it is the only kind a
#:                     bare-button questionnaire ever produces - such a page
#:                     declares no dependencies at all.
#: ``""``            - this question hangs off nothing anyone has observed.
DEPENDS_ON_DECLARED = "declared"
DEPENDS_ON_PROVEN = "proven_reveal"

#: Most triggers listed against ONE child question. A question reachable by
#: several answers is a real shape - three different conditions each revealing
#: the same follow-up - but an unbounded list on a catalogue row is not a
#: report. ``revealed_by_total`` carries the true count, so a clipped list stays
#: visibly clipped: the same discipline ``options_total`` uses for answers.
MAX_REVEALED_BY = 8


def _normalize_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", str(name or "").strip().lower()).strip()


def question_id_for(control: Mapping[str, Any]) -> str:
    """Stable, value-free question id (P2, Δ2).

    Prefers the control SIGNATURE — stable across crawls, so a re-crawl (which
    mints a fresh ``artifact_id``) still maps to the same catalog question and can
    be diffed against the last version. Falls back to the normalized accessible
    name when a control carries no signature. Never contains a user value.
    """
    basis = str(control.get("signature") or "").strip() or _normalize_name(
        str(control.get("name") or ""))
    return "q_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def extract_controls(
    page_state: Mapping[str, Any] | None,
    ledger_by_url: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Extract the control inventory from a single page state.

    Merges ``form_snapshot_signals`` (type, options, required, depends_on)
    with matching ``field_ledger`` entries (signature, semantic_type) by
    field name.  Returns one entry per distinct control.
    """
    if not isinstance(page_state, Mapping):
        return []

    signals = page_state.get("form_snapshot_signals") or {}
    if not isinstance(signals, Mapping):
        signals = {}

    url = str(page_state.get("location") or "")
    ledger_entries = (ledger_by_url or {}).get(url, []) if url else []
    ledger_by_name: dict[str, Mapping[str, Any]] = {}
    for entry in ledger_entries:
        if isinstance(entry, Mapping):
            name = str(entry.get("name") or "").strip()
            if name:
                ledger_by_name[_normalize_name(name)] = entry

    controls: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for field_name, sig_data in signals.items():
        if not isinstance(sig_data, Mapping):
            continue
        name = str(field_name or "").strip()
        if not name:
            continue
        norm = _normalize_name(name)
        if norm in seen_names:
            continue
        seen_names.add(norm)

        ledger_match = ledger_by_name.get(norm, {})

        options = sig_data.get("options")
        if not isinstance(options, list):
            options = []
        options = [str(o) for o in options if str(o).strip()]

        entry: dict[str, Any] = {
            "name": name,
            "type": str(sig_data.get("type") or "text"),
            "options": options,
            "required": bool(sig_data.get("required")),
        }

        # HOW MANY ANSWERS THE PAGE OFFERED, as counted in the page — carried
        # separately from the list so a clipped read stays visible as clipped
        # (M2.2 / T-BR-05). Floored at what we hold: the count may never claim
        # fewer answers than the catalogue can show.
        try:
            declared_total = int(sig_data.get("options_total") or 0)
        except (TypeError, ValueError):
            declared_total = 0
        if declared_total or options:
            entry["options_total"] = max(declared_total, len(options))

        depends_on = sig_data.get("depends_on")
        if depends_on:
            entry["depends_on"] = str(depends_on)

        # WHICH ELEMENT ASKS THIS QUESTION (M2.2 / T-BR-03). Passed through
        # verbatim: every field in it is a handle the page itself declared, and
        # rewriting any of it here would turn captured evidence into this
        # service's opinion. An unverified locator is kept too — "the application
        # identifies this control by nothing" is a finding, not a blank.
        locator = sig_data.get("locator")
        if isinstance(locator, Mapping) and locator:
            entry["locator"] = dict(locator)

        # Validation shape (P2) — HTML constraint attributes, never user values.
        validation = {}
        for vk in _VALIDATION_KEYS:
            vv = sig_data.get(vk)
            if vv not in (None, ""):
                validation[vk] = str(vv)[:80]
        if validation:
            entry["validation"] = validation

        if isinstance(ledger_match, Mapping):
            sig = str(ledger_match.get("signature") or "")
            if sig:
                entry["signature"] = sig
            sem = str(ledger_match.get("semantic_type") or "")
            if sem:
                entry["semantic_type"] = sem
            if not options and isinstance(ledger_match.get("options"), list):
                entry["options"] = [
                    str(o) for o in ledger_match["options"]
                    if str(o).strip()]

        entry["question_id"] = question_id_for(entry)
        controls.append(entry)

    # THE LEDGER FALLBACK IS KEYED BY URL, AND AN SPA SERVES MANY STATES FROM
    # ONE. It exists to catch a field the snapshot missed; on a five-step wizard
    # living at a single URL it instead attributed all five steps' fields to
    # every step. Live that listed most catalogue questions TWICE — once from
    # the state's own signals and once from the shared ledger, under two
    # different question_ids because one basis is the signature and the other
    # the name — and every fallback row claimed type "text" because a ledger
    # entry carries no control type. A client reading that catalogue sees each
    # question duplicated and half of them mistyped.
    #
    # A state that produced signals has already described itself. Only a state
    # with NO signals at all (the snapshot genuinely captured nothing) still
    # needs the ledger to speak for it.
    if not controls:
        for norm_name, ledger_entry in ledger_by_name.items():
            if norm_name in seen_names:
                continue
            seen_names.add(norm_name)
            name = str(ledger_entry.get("name") or "").strip()
            if not name:
                continue
            options = ledger_entry.get("options")
            if not isinstance(options, list):
                options = []
            options = [str(o) for o in options if str(o).strip()]

            entry = {
                "name": name,
                "type": "text",
                "options": options,
                "required": False,
            }
            sig = str(ledger_entry.get("signature") or "")
            if sig:
                entry["signature"] = sig
            sem = str(ledger_entry.get("semantic_type") or "")
            if sem:
                entry["semantic_type"] = sem
            entry["question_id"] = question_id_for(entry)
            controls.append(entry)

    return _fold_question_groups(page_state, controls)


#: The question id of a DECLARED question group. One function, so the fold, the
#: branch rows and the projector's rules cannot drift into three id spaces for
#: one question (M2.1 / T-QT-04).
def group_question_id(group_id: str) -> str:
    return question_id_for({"signature": str(group_id or "").strip()})


def _fold_question_groups(
    page_state: Mapping[str, Any],
    controls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse the members of a declared choice group into ONE question (T-QT-02).

    ``form_snapshot_signals`` is keyed by a control's ACCESSIBLE NAME, and on a
    member of a choice group that name is the name of an ANSWER. So a page asking

        Gender → ( ) Male  ( ) Female

    reached the catalogue as TWO questions, "Male" and "Female", each offering no
    answers at all — the question itself, and every answer it accepts, absent.
    Worse on a health questionnaire: forty radios all named "Yes" or "No" fit in
    a name-keyed dict as exactly two entries, so twenty questions came out as two.

    The crawl has always KNOWN the grouping — the DOM declares it and the
    inventory has stamped it since the Journey Graph — and it simply had no way
    across the service boundary. ``coverage.states[fp].question_groups`` is that
    way (explorer ``inventory.question_groups_of``).

    THE MEMBERS ARE NOT DISCARDED. Each becomes an ``options``/``members`` entry
    on the question, so anything that needs per-option identity (a planned walk
    forcing one answer, a locator for one radio) still has it — it is metadata of
    the question now, not a question of its own.

    Fails OPEN: a manifest from a crawler that predates this key folds nothing
    and behaves exactly as it did before.
    """
    raw_groups = page_state.get("question_groups")
    if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, (str, bytes)):
        return controls

    folded: list[dict[str, Any]] = []
    member_names: dict[str, str] = {}     # normalized member name → group_id
    by_gid: dict[str, dict[str, Any]] = {}
    for g in raw_groups:
        if not isinstance(g, Mapping):
            continue
        gid = str(g.get("group_id") or "").strip()
        if not gid or gid in by_gid:
            continue
        options = [str(o).strip() for o in (g.get("options") or []) if str(o).strip()]
        members = [
            {"name": str(m.get("name") or "")[:200],
             "kind": str(m.get("kind") or "")[:24]}
            for m in (g.get("members") or []) if isinstance(m, Mapping)
            and str(m.get("name") or "").strip()]
        label = str(g.get("label") or "").strip()
        row: dict[str, Any] = {
            # THE APPLICATION'S OWN WORDING, or nothing. An empty name here is
            # not a gap in the capture — it is the finding that this application
            # states no question for these controls, and ``build_master_catalog``
            # publishes it as UNVERIFIED rather than filling it in.
            "name": label[:200],
            "name_source": str(g.get("label_source") or "")[:40],
            "type": str(g.get("type") or "radio")[:40],
            "options": options,
            "options_total": max(
                len(options), _as_int(g.get("options_total"))),
            "required": bool(g.get("required")),
            # Identity is the DECLARED GROUP, never the wording — so re-reading
            # a reworded question does not re-key it, and an unverified one is
            # still stably identified.
            "signature": gid,
            "question_id": group_question_id(gid),
            "members": members,
            "source": "question_group",
        }
        by_gid[gid] = row
        folded.append(row)
        for m in members:
            member_names.setdefault(_normalize_name(m["name"]), gid)

    if not folded:
        return controls

    #: Kinds whose members answer a group question. A ``select`` is its own
    #: question and must never be folded away by sharing a label with a radio.
    grouped_kinds = {"radio", "checkbox", "toggle", "button"}
    kept: list[dict[str, Any]] = []
    for c in controls:
        gid = member_names.get(_normalize_name(str(c.get("name") or "")))
        if gid and str(c.get("type") or "") in grouped_kinds:
            # Fold the member's own evidence onto its question rather than
            # dropping it: the member row is where the field ledger's semantic
            # type and the page's locator landed.
            row = by_gid[gid]
            if not row.get("semantic_type") and c.get("semantic_type"):
                row["semantic_type"] = str(c["semantic_type"])
            row["required"] = bool(row["required"]) or bool(c.get("required"))
            for m in row["members"]:
                if _normalize_name(m["name"]) == _normalize_name(str(c.get("name") or "")):
                    if c.get("signature"):
                        m["signature"] = str(c["signature"])
                    if isinstance(c.get("locator"), Mapping) and c["locator"]:
                        m["locator"] = dict(c["locator"])
            continue
        kept.append(c)
    return folded + kept


def _as_int(value: Any) -> int:
    try:
        return int(value) if value is not None and not isinstance(value, bool) else 0
    except (TypeError, ValueError):
        return 0


# ─── M2.3 · QUESTION LIFECYCLE ───────────────────────────────────────────────
# A catalogue that only ever grows is not a record of an application, it is a
# record of everything the application has ever been — and it cannot answer the
# one question a regulated client asks after a release: "what did you STOP
# asking?". Until this existed the catalogue had no way to say it: node control
# inventories are merged by :func:`merge_controls`, which is a UNION, so a
# control that disappeared from the application stayed in the inventory for ever,
# every later snapshot still contained it, and ``catalog_diff``'s ``removed``
# bucket was structurally unreachable from a real crawl.
#
# THE STATES. A question is in exactly one of these:
#
#   active   — observed by the crawl that last looked for it.
#   stale    — previously known, NOT observed, and the absence is not yet
#              conclusive. Still catalogued, still planned against, flagged.
#   retired  — previously known, not observed, and the absence IS conclusive.
#              Kept for audit with its retirement timestamp and the crawl that
#              retired it; excluded from the active catalogue, and therefore
#              reported by the diff as ``removed``.
#
# Nothing is ever deleted. Retirement is REVERSIBLE: a question observed again by
# a later crawl is revived to ``active`` and its retirement record cleared,
# because the application asking it again is exactly the evidence that says the
# retirement was wrong.
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_STALE = "stale"
LIFECYCLE_RETIRED = "retired"

#: How many INCONCLUSIVE crawls may miss a question before its repeated absence
#: becomes evidence in its own right. A single crawl that ran degraded (auth
#: incomplete, a page that would not read) is weather; the same question missing
#: from two independent degraded crawls is a signal. A CONCLUSIVE crawl needs no
#: threshold — see :func:`apply_control_lifecycle`.
RETIREMENT_MISS_THRESHOLD = 2

#: Keys :func:`apply_control_lifecycle` owns on a control-inventory entry. Listed
#: so a revival clears exactly the lifecycle record and nothing else.
_LIFECYCLE_KEYS = ("stale", "retired_at", "retired_in_crawl", "missed_crawls",
                   "last_seen_crawl", "retire_reason")


def observed_question_ids(
    page_state: Mapping[str, Any] | None,
    ledger_by_url: Mapping[str, list[Mapping[str, Any]]] | None = None,
) -> set[str]:
    """The question ids ONE page state actually asked, in the catalogue's id space.

    Derived through :func:`extract_controls` and :func:`question_id_for` — the
    same two functions the catalogue itself is built with — so "observed in this
    crawl" and "present in the catalogue" are the same identity by construction,
    rather than by a parallel rule that can drift out of step with it and start
    retiring questions that never went anywhere.
    """
    return {
        qid for qid in (
            str(c.get("question_id") or "") or question_id_for(c)
            for c in extract_controls(page_state, ledger_by_url)
        ) if qid
    }


def crawl_evidence(coverage: Mapping[str, Any] | None) -> dict[str, Any]:
    """Is this crawl's account strong enough to RETIRE a question on?

    Absence of evidence is not evidence of absence. A crawl that could not sign
    in, or could not read a page it reached, did not observe the application — it
    observed a degraded view of it, and every question it "missed" may be sitting
    behind the wall it never got through. Retiring on that account is how a
    catalogue starts lying.

    ``conclusive`` is therefore False whenever the crawl told us its own view was
    partial. Every reason is the crawl's OWN self-report, never an inference:

      * ``no_states``          — it observed nothing at all.
      * ``no_flows``           — it recorded states but walked no journey, so it
                                 saw entry snapshots rather than an application.
      * ``auth_blocked``       — it was refused at the login wall, so the
                                 authenticated surface is entirely unobserved.
      * ``inventory_failures`` — pages it reached would not read (M1.7 T-GW-01).
                                 A page that failed to read is not a page whose
                                 questions are gone.
      * ``pre_hardening``      — a manifest from a crawler predating the
                                 Release-A/B gates, whose completeness the fold
                                 cannot vouch for.

    WHY ``auth_incomplete`` IS NOT IN THAT LIST, though it was at first. It is a
    statement about COVERAGE BREADTH — "authenticated areas not covered" — and
    not about the trustworthiness of what the crawl did read. Breadth is already
    handled, and handled more precisely, one page at a time: a node the crawl
    never reached is never evaluated for retirement at all (see
    :func:`apply_control_lifecycle`), so counting it again here is double
    counting with a much blunter instrument.

    And the blunt instrument was measured to be wrong. Two real crawls of
    ``proving-grounds/acme-life`` — which keeps its signed-in user in
    sessionStorage, as a large class of SPAs does — both report
    ``auth_incomplete`` with reason ``not_persisted``: the login verified, the
    app dropped it on the next page load, and the crawler recovered by
    continuing in place. Both crawls then walked the funnel and read the
    application form. Treating that as inconclusive would mean no SPA holding
    its session in client state could ever retire a question, which is most of
    the applications this product exists for.

    ``auth_blocked`` stays: that one says the crawl never got in at all.

    An inconclusive crawl still MARKS what it missed — that is ``stale`` — it
    just may not retire it on its own authority.
    """
    if not isinstance(coverage, Mapping):
        return {"conclusive": False, "reason": "no_coverage"}
    reasons: list[str] = []
    states = coverage.get("states")
    if not isinstance(states, list) or not states:
        reasons.append("no_states")
    flows = coverage.get("flows")
    if not isinstance(flows, list) or not flows:
        reasons.append("no_flows")
    if coverage.get("auth_blocked"):
        reasons.append("auth_blocked")
    try:
        if int(coverage.get("inventory_failures") or 0) > 0:
            reasons.append("inventory_failures")
    except (TypeError, ValueError):
        reasons.append("inventory_failures_unreadable")
    summary = coverage.get("flow_summary")
    if not (isinstance(summary, Mapping) and "advances_by_tier" in summary):
        reasons.append("pre_hardening")
    return {"conclusive": not reasons, "reason": ",".join(reasons)}


def _clear_lifecycle(entry: dict[str, Any], crawl_ref: str) -> None:
    """Revive a control: it was observed, so it is active again."""
    for key in _LIFECYCLE_KEYS:
        entry.pop(key, None)
    entry["stale"] = False
    entry["missed_crawls"] = 0
    if crawl_ref:
        entry["last_seen_crawl"] = crawl_ref


def apply_control_lifecycle(
    inventory: Sequence[Mapping[str, Any]] | None,
    observed: set[str] | frozenset[str],
    *,
    crawl_ref: str,
    now_iso: str,
    conclusive: bool,
) -> list[dict[str, Any]]:
    """Stamp the lifecycle of ONE node's control inventory against one crawl.

    Call this ONLY for a node the crawl actually visited: ``observed`` is that
    visit's question ids, so a control's absence from it means "the crawl looked
    at this page and the question was not there". For a node the crawl never
    reached there is nothing to conclude and this must not be called — silence
    about a page is not a report about it.

    Retirement is evidenced two ways, and which one is recorded in
    ``retire_reason``:

      * ``conclusive_absence`` — a conclusive crawl (see :func:`crawl_evidence`)
        re-read the page and the question was gone. ONE such crawl is enough:
        waiting for a second would leave a client's catalogue knowingly wrong for
        a whole release cycle, to guard against a failure mode the evidence rule
        has already excluded.
      * ``repeated_absence``   — ``RETIREMENT_MISS_THRESHOLD`` inconclusive crawls
        re-read the page without seeing it. No single one of them was trusted;
        their agreement is what carries.

    Nothing is dropped. The returned list holds every entry the inventory held,
    in order, retired ones included.
    """
    out: list[dict[str, Any]] = []
    for ctrl in (inventory or []):
        if not isinstance(ctrl, Mapping):
            continue
        entry = dict(ctrl)
        qid = str(entry.get("question_id") or "") or question_id_for(entry)
        entry["question_id"] = qid
        if qid in observed:
            _clear_lifecycle(entry, crawl_ref)
            out.append(entry)
            continue

        # Absent from a page the crawl DID read.
        try:
            misses = int(entry.get("missed_crawls") or 0)
        except (TypeError, ValueError):
            misses = 0
        misses += 1
        entry["stale"] = True
        entry["missed_crawls"] = misses
        if not entry.get("retired_at"):
            if conclusive:
                entry["retired_at"] = now_iso
                entry["retired_in_crawl"] = crawl_ref
                entry["retire_reason"] = "conclusive_absence"
            elif misses >= RETIREMENT_MISS_THRESHOLD:
                entry["retired_at"] = now_iso
                entry["retired_in_crawl"] = crawl_ref
                entry["retire_reason"] = "repeated_absence"
        out.append(entry)
    return out


def control_lifecycle_state(control: Mapping[str, Any]) -> str:
    """The lifecycle state of ONE control-inventory entry."""
    if not isinstance(control, Mapping):
        return LIFECYCLE_ACTIVE
    if control.get("retired_at"):
        return LIFECYCLE_RETIRED
    return LIFECYCLE_STALE if control.get("stale") else LIFECYCLE_ACTIVE


#: Ceiling on the answers stored per catalogued question. Sized for the
#: enumerations business forms actually ask — 50 US states, ~250 countries, a
#: 100-year date-of-birth range. The previous 48 clipped the states themselves,
#: and the clip was silent: the catalogue simply held a shorter list, so every
#: case generated from it claimed to cover a question it only partly knew.
#: Bounded on purpose — one pathological control must not dominate a snapshot.
MAX_CATALOG_OPTIONS = 300


def _catalog_options(control: Mapping[str, Any]) -> list[str]:
    """The answer labels to store for one question, deduped and bounded."""
    out: list[str] = []
    seen: set[str] = set()
    for o in (control.get("options") or []):
        label = str(o).strip()
        if not label or label in seen:
            continue
        seen.add(label)
        out.append(label)
        if len(out) >= MAX_CATALOG_OPTIONS:
            break
    return out


def _options_total(control: Mapping[str, Any]) -> int:
    """How many answers the question OFFERS, as observed in the page.

    Floored at the number actually stored so the catalogue can never claim fewer
    answers than it holds, and carried separately from the list so a clipped
    enumeration stays visible as clipped. A question whose stored options are a
    PREFIX is still a useful catalogue row; a prefix that PRESENTS as the whole
    answer set is a fabrication, and the difference is this number.
    """
    stored = len(_catalog_options(control))
    raw = control.get("options_total")
    try:
        declared = int(raw) if raw is not None and not isinstance(raw, bool) else 0
    except (TypeError, ValueError):
        declared = 0
    return max(declared, stored, len(control.get("options") or []))


#: Whether a catalogue question can be pointed at an element on the page.
#:
#:   * ``verified``   — the crawl captured a handle the page declared AND proved
#:     it resolves to exactly one control (see ``inventory.attach_locators`` in
#:     qe-explorer, which is the only place with the whole page in view).
#:   * ``UNVERIFIED`` — the crawl captured the control but the application
#:     identifies it by nothing usable, or two controls share the only handle it
#:     has. A finding about the application.
#:   * ``absent``     — no locator crossed the boundary for this question at all.
#:     Structurally different from the two above: a questionnaire question folded
#:     in from a BRANCH row is a control signature and an option label, never an
#:     element, so it can have no locator and is not being accused of lacking one.
LOCATOR_STATE_VERIFIED = "verified"
LOCATOR_STATE_UNVERIFIED = "UNVERIFIED"
LOCATOR_STATE_ABSENT = "absent"


def locator_state_of(question: Mapping[str, Any]) -> str:
    """The locator honesty marker for one catalogue question."""
    loc = question.get("locator")
    if not isinstance(loc, Mapping) or not loc:
        return LOCATOR_STATE_ABSENT
    if loc.get("verified") and str(loc.get("value") or "").strip():
        return LOCATOR_STATE_VERIFIED
    return LOCATOR_STATE_UNVERIFIED


#: Whether a catalogue question carries a business rule the CRAWL PROVED.
#:
#: ``UNVERIFIED`` is not a failure state and not a placeholder for a rule we
#: expect to arrive later — it is the correct, final answer for the great
#: majority of questions in any application, because most questions gate
#: nothing. It is spelled in capitals, and written on every question that has
#: no rule, so a reader is never left to interpret a blank field. A blank is
#: ambiguous between "no rule" and "we did not look"; those are different
#: findings and only one of them is about the application.
#:
#: Nothing in this module INFERS a rule. A rule reaches a question by exactly
#: one route: an experiment in which the crawl answered a declined question, the
#: application enabled its own forward control, and the sentence describing that
#: was persisted against (tenant, app). Everything else is UNVERIFIED.
RULE_STATE_OBSERVED = "observed"
RULE_STATE_UNVERIFIED = "UNVERIFIED"

#: How a rule was established. One value today, named rather than implied so a
#: second kind of evidence (a value range the application rejected, a
#: cross-field constraint it enforced) can be added without a consumer having to
#: guess whether an unfamiliar rule was observed or invented.
RULE_SOURCE_CRAWL_EXPERIMENT = "crawl_experiment"


def index_rules_by_field(
    rules: Sequence[Mapping[str, Any]] | None,
) -> dict[str, list[Mapping[str, Any]]]:
    """``{normalised field label: [rule, …]}`` from the durable rule store.

    The join key is the FIELD LABEL, because that is what a discovered rule is
    about: ``field_label`` names the question whose answer released the block,
    and the catalogue keys questions by the same accessible name. ``blocked_label``
    names the control the rule gates — a Continue button, which is not a
    catalogue question and must never be mistaken for one.

    Order is preserved from the caller, which reads newest-proven first, so a
    field gated on more than one page keeps its freshest sentence at the front.
    """
    by_field: dict[str, list[Mapping[str, Any]]] = {}
    for rule in (rules or ()):
        if not isinstance(rule, Mapping):
            continue
        field = _normalize_name(str(rule.get("field_label") or ""))
        proof = str(rule.get("proof") or "").strip()
        if not field or not proof:
            # A rule with no sentence proves nothing a reader can act on, and
            # attaching it would put a question into the "has a business rule"
            # count with nothing to show for it.
            continue
        by_field.setdefault(field, []).append(rule)
    return by_field


def _attach_business_rule(
    row: dict[str, Any], rules_by_field: Mapping[str, list[Mapping[str, Any]]],
) -> None:
    """Stamp ``row`` with the rule the crawl proved about it, or UNVERIFIED.

    Called for every row, including rows with no rule — that is the point. A
    field written only where a rule exists leaves the reader of a rule-less
    question unable to distinguish it from a question this build never examined.
    """
    matches = rules_by_field.get(_normalize_name(row.get("name") or "")) or []
    if not matches:
        row["business_rule"] = ""
        row["business_rule_state"] = RULE_STATE_UNVERIFIED
        return
    rule = matches[0]
    row["business_rule"] = str(rule.get("proof") or "")[:500]
    row["business_rule_state"] = RULE_STATE_OBSERVED
    evidence: dict[str, Any] = {
        "source": RULE_SOURCE_CRAWL_EXPERIMENT,
        "kind": str(rule.get("kind") or "")[:32],
        "rule_key": str(rule.get("key") or "")[:64],
        # WHAT THE RULE GATES. Without it the sentence is the only record of
        # which control the application refused to enable, and a sentence is not
        # a field a consumer can filter on.
        "gates": str(rule.get("blocked_label") or "")[:120],
        "url_template": str(rule.get("url_template") or "")[:500],
    }
    if len(matches) > 1:
        # The same question gates different controls on different pages. Counted
        # rather than concatenated: one row may hold one sentence honestly, and
        # a reader who sees 3 here knows to ask the rule store for the rest.
        evidence["also_proven"] = len(matches) - 1
    row["business_rule_evidence"] = evidence


def apply_reveal_dependencies(
    questions: Sequence[Mapping[str, Any]],
    reveal_rules: Sequence[Mapping[str, Any]] | None,
) -> int:
    """Join the P1 trigger->child rules ONTO the questions they are about.

    THE HOLE THIS FILLS. ``journey_projector.rules_from_branches`` has resolved
    "answering X with 'Yes' reveals Y" since P1, and exactly ONE caller has ever
    read it: the persona projector, which uses it to PREDICT a path. The
    catalogue - the deliverable an operator actually reads, and the thing a
    client is handed - never saw it. So a question that exists only because
    another question was answered a particular way was published with
    ``depends_on`` empty, indistinguishable in the artifact from a question the
    application asks unconditionally.

    ``depends_on`` was not even an absent field. qec_019 added the column and
    documented it as "the question whose answer this one hangs off (ACT-THEN-DIFF
    proven)" - and the only thing that ever wrote it was the page's own DECLARED
    signal. The proven half was captured by the branch walk, resolved by the
    projector, and dropped one function short of the catalogue.

    A DECLARED DEPENDENCY IS NEVER OVERWRITTEN. Where the page states what a
    field hangs off, that statement stands and the reveal is recorded beside it
    in ``revealed_by``. The two can legitimately disagree - an application may
    declare ``depends_on`` naming one control while a different control is what
    actually reveals it - and collapsing them would destroy the disagreement,
    which is the part worth reading.

    Mutates ``questions`` in place (they are the dicts this module just built)
    and returns how many gained a PROVEN dependency they did not have.
    """
    by_qid: dict[str, Any] = {}
    for q in questions:
        qid = str(q.get("question_id") or "")
        if qid:
            by_qid[qid] = q

    triggers: dict[str, list[tuple[str, str, str]]] = {}
    for rule in (reveal_rules or ()):
        if not isinstance(rule, Mapping):
            continue
        trigger_qid = str(rule.get("question_id") or "")
        if not trigger_qid:
            continue
        parent = by_qid.get(trigger_qid)
        # A TRIGGER THE CATALOGUE DOES NOT HOLD cannot be named to a reader, and
        # "this question depends on one we cannot show you" is not a report.
        # Dropped rather than half-written - the same posture
        # ``rules_from_branches`` already takes on a child it cannot resolve.
        if parent is None:
            continue
        parent_name = str(parent.get("name") or "")
        option = str(rule.get("option") or "")
        for child_qid in (rule.get("reveals_question_ids") or ()):
            cqid = str(child_qid or "")
            if not cqid or cqid == trigger_qid or cqid not in by_qid:
                continue
            triggers.setdefault(cqid, []).append((trigger_qid, parent_name, option))

    gained = 0
    for cqid, found in triggers.items():
        child = by_qid[cqid]
        # DETERMINISTIC. This rides into a durable row and into an API response,
        # so two reads of one database must not order it differently, and a
        # re-fold must not churn the snapshot the next diff is taken against.
        uniq = sorted(set(found), key=lambda t: (t[1], t[2], t[0]))
        child["revealed_by"] = [
            {"question_id": t[0], "question": t[1], "option": t[2]}
            for t in uniq[:MAX_REVEALED_BY]
        ]
        child["revealed_by_total"] = len(uniq)
        if str(child.get("depends_on") or "").strip():
            child["depends_on_source"] = DEPENDS_ON_DECLARED
            continue
        # The trigger has to be NAMEABLE. An UNVERIFIED-name question can still
        # reveal a child - the reveal was observed either way - but writing an
        # empty string into ``depends_on`` would say "depends on nothing", which
        # is the opposite of what was proven. ``revealed_by`` above still
        # carries the trigger's id, so the relationship is not lost.
        named = next((t[1] for t in uniq if t[1]), "")
        if not named:
            continue
        child["depends_on"] = named[:200]
        child["depends_on_source"] = DEPENDS_ON_PROVEN
        gained += 1
    return gained


def build_master_catalog(
    nodes: Sequence[Mapping[str, Any]],
    edges: Sequence[Mapping[str, Any]] | None = None,
    branches: Sequence[Mapping[str, Any]] | None = None,
    rules: Sequence[Mapping[str, Any]] | None = None,
    include_retired: bool = False,
    reveal_rules_fn: Any = None,
) -> dict[str, Any]:
    """Aggregate per-node control inventories into ONE app-scoped Master Catalog.

    The per-node/per-journey catalog answers "what is on this page"; the Master
    Catalog answers "what questions does this APPLICATION have", deduped by stable
    ``question_id`` across every journey and node (Δ2) — the same question on two
    journeys is one row, not two, so the 400 questions are never duplicated per
    journey. Each row records every page it was seen on and keeps the richest
    metadata observed. ``expected_next_page`` is filled from the journey edges when
    available. Pure: a DB reader loads the nodes/edges/branches and calls this.

    ``nodes`` items: ``{node_fp|fingerprint, url, title, controls|controls_inventory}``.
    ``edges`` items: ``{from_fp, to_fp}`` (highest-priority next page per node).
    ``branches`` items: ``{node_fp, control_signature, control_label_norm,
    option_label_norm}`` — questionnaire questions (bare Yes/No etc.) live as
    branch rows, not form fields, so they are folded in here too. Their
    ``question_id`` is derived from the control SIGNATURE, the same id space the
    persona projector's trigger→child rules use — so P1 branch rules join the
    catalog cleanly.

    ``rules`` items are the durable business rules earlier crawls of this app
    PROVED (``rule_store.fetch_rules`` shape). Joined by field label, so the
    question whose answer released a block carries the sentence the application
    justified. Every question the rules do not name is stamped
    ``business_rule_state = UNVERIFIED`` — see :data:`RULE_STATE_UNVERIFIED`.
    Omitting ``rules`` is legitimate (a caller that only wants shape) and yields
    a catalogue in which every question is UNVERIFIED, which is exactly what a
    build with no rule evidence should say.

    ``reveal_rules_fn`` closes the P1 trigger->child loop onto the catalogue.
    It is a CALLABLE taking the assembled questions and returning the projector's
    rules (``journey_projector.rules_from_branches`` partially applied over the
    same ``branches``), because the rules can only be resolved once the questions
    exist and this module must stay dependency-free — ``journey_projector``
    imports FROM here, so importing it back would be a cycle. Omitting it is
    legitimate and yields a catalogue whose only dependencies are the ones the
    pages declared, which is exactly what a build with no branch evidence should
    say. See :func:`apply_reveal_dependencies`.

    LIFECYCLE (M2.3). Every question carries ``lifecycle`` — ``active``,
    ``stale`` or ``retired`` (see :data:`LIFECYCLE_ACTIVE`). A question is
    aggregated across every page it lives on, so it is only as retired as its
    LAST surviving sighting: still present on one page and gone from another, it
    is active, and its ``pages`` shrink. ``retired_at`` / ``retired_in_crawl``
    are taken from the sighting that retired last — the moment the application
    stopped asking it anywhere.

    ``include_retired`` decides which catalogue this is:

      * **False** (default) — the ACTIVE catalogue. Retired questions are absent.
        This is what planning, scenario derivation and the version snapshot read,
        and it is what makes ``catalog_diff``'s ``removed`` bucket reachable: a
        question the application stopped asking leaves the new snapshot, so the
        diff names it.
      * **True** — the AUDIT catalogue: every question this application has ever
        asked, retired ones included and labelled. Nothing is ever deleted, so
        this view can always answer "what did we stop asking, and when".

    ``summary`` counts both, always — a reader of the active catalogue is told
    how many questions are being withheld from it rather than left to infer that
    none are.
    """
    rules_by_field = index_rules_by_field(rules)
    next_by_node: dict[str, str] = {}
    page_by_fp: dict[str, str] = {}
    for e in (edges or []):
        if not isinstance(e, Mapping):
            continue
        frm, to = str(e.get("from_fp") or ""), str(e.get("to_fp") or "")
        if frm and to and frm not in next_by_node:
            next_by_node[frm] = to

    # PAGE IDENTITY IS THE URL, NOT THE TITLE. A single-page application serves
    # every step of a wizard under ONE <title>, so a title-first label collapsed
    # every question in the application onto one "page": live, all 24 catalogued
    # questions read as ["Summit Life Carrier Administration"]. The catalogue
    # could not say which step a question belonged to — the first thing anyone
    # asks of it — and "questions per step" was unrepresentable.
    #
    # Where one URL genuinely carries several distinct states (an SPA wizard is
    # exactly that), the state fingerprint disambiguates. Only where it must:
    # an ordinary page keeps a clean, legible URL as its label.
    url_counts: dict[str, int] = {}
    for node in nodes:
        if isinstance(node, Mapping):
            u = str(node.get("url") or "")
            if u:
                url_counts[u] = url_counts.get(u, 0) + 1

    by_qid: dict[str, dict[str, Any]] = {}
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_fp = str(node.get("node_fp") or node.get("fingerprint") or "")
        url = str(node.get("url") or "")
        if url:
            page = (url[:200] if url_counts.get(url, 0) < 2
                    else f"{url[:180]}#{node_fp[:12]}")
        else:
            page = str(node.get("title") or node_fp or "")[:200]
        if node_fp:
            page_by_fp[node_fp] = page
        controls = node.get("controls")
        if controls is None:
            controls = node.get("controls_inventory")
        if not isinstance(controls, Sequence) or isinstance(controls, (str, bytes)):
            continue
        for c in controls:
            if not isinstance(c, Mapping):
                continue
            qid = str(c.get("question_id") or "") or question_id_for(c)
            # THE SIGHTING'S OWN LIFECYCLE, folded in below. Read before the row
            # is built so a question first met as a RETIRED entry (its only page
            # dropped it) still enters the catalogue carrying its retirement,
            # rather than being born active and silently resurrected.
            _life = control_lifecycle_state(c)
            row = by_qid.get(qid)
            if row is None:
                row = {
                    "question_id": qid,
                    "name": str(c.get("name") or "")[:200],
                    "type": str(c.get("type") or "text"),
                    "options": _catalog_options(c),
                    # How many answers the question OFFERS. Differs from
                    # len(options) only when a read was clipped, and that
                    # difference is exactly what a consumer must be able to see.
                    "options_total": _options_total(c),
                    "required": bool(c.get("required")),
                    "semantic_type": str(c.get("semantic_type") or ""),
                    "provenance": str(c.get("provenance") or PROVENANCE_OBSERVED),
                    "pages": [],
                    # Lifecycle tally across sightings — resolved after the loop.
                    "_live": 0, "_seen": 0, "_retirements": [], "_missed": 0,
                }
                val = c.get("validation")
                if isinstance(val, Mapping) and val:
                    row["validation"] = dict(val)
                # WHICH OTHER QUESTION THIS ONE HANGS OFF (M2.2 / T-BR-02). The
                # crawler's ACT-THEN-DIFF pass proves it — a select that offered
                # nothing until a driver was answered, a field that did not exist
                # until one was — and ``form_signal_for`` and ``extract_controls``
                # have both carried it for a while. THIS builder dropped it: the
                # row was assembled field by field and ``depends_on`` was not one
                # of the fields, so a dependency survived every layer up to the
                # one that composes the deliverable and died there. Live, the
                # Master Catalog and ``catalog_questions`` held no dependency at
                # all, and a conditional question read as an unconditional one —
                # which is a false statement about the application, not a gap.
                dep = str(c.get("depends_on") or "").strip()
                if dep:
                    row["depends_on"] = dep[:200]
                loc = c.get("locator")
                if isinstance(loc, Mapping) and loc:
                    row["locator"] = dict(loc)
                # PER-OPTION IDENTITY, KEPT AS METADATA OF THE QUESTION (M2.1).
                # A radio group is ONE question, and everything that used to
                # need a per-member row - forcing one answer on a planned walk,
                # a locator for one radio - still needs the member. It is a
                # ``members`` entry of the question now, not a question of its
                # own, which is the whole difference between a catalogue that
                # describes a form and one that lists its input elements.
                members = c.get("members")
                if isinstance(members, Sequence) and not isinstance(
                        members, (str, bytes)) and members:
                    row["members"] = [dict(m) for m in members
                                      if isinstance(m, Mapping)][:MAX_CATALOG_OPTIONS]
                src_kind = str(c.get("source") or "").strip()
                if src_kind:
                    row["source"] = src_kind[:40]
                nsrc = str(c.get("name_source") or "").strip()
                if nsrc:
                    row["name_source"] = nsrc[:40]
                nxt = next_by_node.get(node_fp)
                if nxt:
                    row["expected_next_page"] = nxt
                by_qid[qid] = row
            else:
                # Keep the richest observation: required is sticky-True, fill in
                # options/validation if this sighting has them and the row didn't.
                row["required"] = row["required"] or bool(c.get("required"))
                # RICHEST WINS. The same question is met on several pages and one
                # sighting may be more complete than another — a dependent
                # dropdown is empty until its driver is answered, so the first
                # sighting of "County" can legitimately offer nothing while a
                # later one offers forty. Keeping the first observation meant the
                # catalogue held the emptiest view of every dependent question.
                incoming = _catalog_options(c)
                if len(incoming) > len(row["options"]):
                    row["options"] = incoming
                row["options_total"] = max(
                    int(row.get("options_total") or 0), _options_total(c))
                if "validation" not in row and isinstance(c.get("validation"), Mapping):
                    if c["validation"]:
                        row["validation"] = dict(c["validation"])
                # A dependency is proven by the ONE sighting that watched the
                # driver populate it; the other sightings simply never saw the
                # experiment. So a later sighting may ADD the dependency and must
                # never clear it — "richest wins" means the same thing here as it
                # does for options.
                if "depends_on" not in row:
                    dep = str(c.get("depends_on") or "").strip()
                    if dep:
                        row["depends_on"] = dep[:200]
                # THE WORDING IS EVIDENCE LIKE ANY OTHER, and one sighting may
                # carry it where another did not - a question whose <legend> is
                # rendered only once its section expands, a group first met
                # collapsed. Filling an EMPTY name from a later sighting is the
                # same "richest wins" rule as options. A name already read is
                # never overwritten: identity does not depend on it, and
                # churning it would make a re-crawl look like a rewording.
                if not row.get("name"):
                    incoming_name = str(c.get("name") or "").strip()
                    if incoming_name:
                        row["name"] = incoming_name[:200]
                        nsrc = str(c.get("name_source") or "").strip()
                        if nsrc:
                            row["name_source"] = nsrc[:40]
                if not row.get("members"):
                    members = c.get("members")
                    if isinstance(members, Sequence) and not isinstance(
                            members, (str, bytes)) and members:
                        row["members"] = [dict(m) for m in members
                                          if isinstance(m, Mapping)][:MAX_CATALOG_OPTIONS]
                # LOCATORS DO NOT MERGE ACROSS SIGHTINGS THE WAY OPTIONS DO.
                # Every control folded into this row is the same question by
                # ``question_id``, so any of their handles identifies it — but
                # only an UPGRADE is taken (nothing → something, unverified →
                # verified). Overwriting a verified handle with a later
                # unverified one would lose evidence, and taking the newest
                # unconditionally is how one member of a radio group ends up
                # wearing a sibling's locator.
                incoming_loc = c.get("locator")
                if isinstance(incoming_loc, Mapping) and incoming_loc:
                    held = row.get("locator")
                    if (not isinstance(held, Mapping) or not held
                            or (bool(incoming_loc.get("verified"))
                                and not bool(held.get("verified")))):
                        row["locator"] = dict(incoming_loc)
            row["_seen"] = int(row.get("_seen") or 0) + 1
            try:
                row["_missed"] = max(int(row.get("_missed") or 0),
                                     int(c.get("missed_crawls") or 0))
            except (TypeError, ValueError):
                pass
            if _life == LIFECYCLE_RETIRED:
                row["_retirements"].append({
                    "retired_at": str(c.get("retired_at") or ""),
                    "retired_in_crawl": str(c.get("retired_in_crawl") or ""),
                    "retire_reason": str(c.get("retire_reason") or ""),
                    "last_seen_crawl": str(c.get("last_seen_crawl") or ""),
                })
            else:
                if _life == LIFECYCLE_ACTIVE:
                    row["_live"] = int(row.get("_live") or 0) + 1
                # A PAGE THE QUESTION HAS LEFT IS NOT A PAGE IT IS ON. Retired
                # sightings keep their retirement record but not their page: the
                # active catalogue's ``pages`` must name where the question is
                # asked TODAY, or a client is sent to a screen that no longer
                # has it.
                if page and page not in row["pages"]:
                    row["pages"].append(page)

    # Fold in questionnaire questions (branch rows): a question per distinct
    # control signature, its options accumulated across its branch rows. Keyed by
    # ``question_id_for({"signature": ...})`` so the projector's rules join here.
    for b in (branches or []):
        if not isinstance(b, Mapping):
            continue
        sig = str(b.get("control_signature") or "")
        label = str(b.get("control_label_norm") or b.get("control_label") or "")
        if not sig and not label:
            continue
        qid = question_id_for({"signature": sig, "name": label})
        opt = str(b.get("option_label_norm") or b.get("option") or "")
        node_fp = str(b.get("node_fp") or "")
        # ONE BRANCH ROW IS ONE ANSWER, so a branch-sourced question retires only
        # when every answer it offered has. An option the application withdrew
        # drops out of ``options`` while the question lives on — which the diff
        # reports as ``options_changed``, the honest description of it.
        _life = control_lifecycle_state(b)
        row = by_qid.get(qid)
        if row is None:
            row = {
                "question_id": qid,
                "name": label[:200],
                "type": "choice",
                "options": [],
                "required": False,
                "semantic_type": "",
                "provenance": PROVENANCE_OBSERVED,
                "pages": [],
                "source": "branch",
                "_live": 0, "_seen": 0, "_retirements": [], "_missed": 0,
            }
            nxt = next_by_node.get(node_fp)
            if nxt:
                row["expected_next_page"] = nxt
            by_qid[qid] = row
        # THE BRANCH AND THE CONTROL ARE THE SAME QUESTION (M2.1 / T-QT-04).
        # A choice group's branch rows are keyed on the DOM's declared group id,
        # which is exactly the signature its folded control row carries, so both
        # land on this one ``qid``. The branch therefore contributes its ANSWERS
        # to the question instead of minting a second question named after one
        # of them - which is what a "Gender" question looked like before: two
        # member rows called Male and Female, plus a third called male.
        #
        # It may also be the sighting that carries the wording (a questionnaire
        # question exists ONLY as branch rows), so an empty name is filled here
        # and a name already read is left alone.
        if not row.get("name") and label:
            row["name"] = label[:200]
        row["_seen"] = int(row.get("_seen") or 0) + 1
        try:
            row["_missed"] = max(int(row.get("_missed") or 0),
                                 int(b.get("missed_crawls") or 0))
        except (TypeError, ValueError):
            pass
        if _life == LIFECYCLE_RETIRED:
            row["_retirements"].append({
                "retired_at": str(b.get("retired_at") or ""),
                "retired_in_crawl": str(b.get("retired_in_crawl") or ""),
                "retire_reason": str(b.get("retire_reason") or ""),
                "last_seen_crawl": str(b.get("last_seen_crawl") or ""),
            })
            continue
        if _life == LIFECYCLE_ACTIVE:
            row["_live"] = int(row.get("_live") or 0) + 1
        # ONE ANSWER, ONE ENTRY. A branch row carries the option NORMALIZED
        # ("yes"), while the control sighting carries the page's own casing
        # ("Yes"). Both describe the same answer, and now that they fold into
        # one question a plain membership test listed it twice - a "Do you use
        # tobacco?" offering ["Yes", "No", "yes", "no"], i.e. a catalogue
        # claiming the application accepts four answers where it accepts two.
        # The page's own casing is the one worth keeping, so an answer already
        # held wins and the normalized form is only added when nothing matches.
        if opt and not any(str(o).strip().lower() == opt.strip().lower()
                           for o in row["options"]):
            row["options"].append(opt)
        page = page_by_fp.get(node_fp) or node_fp
        if page and page not in row["pages"]:
            row["pages"].append(page)

    all_questions = sorted(
        by_qid.values(), key=lambda r: (r.get("name") or "", r["question_id"]))

    # EVERY row is stamped, including the ones with nothing to stamp. Running
    # this over all questions rather than only over the matched ones is what
    # makes ``UNVERIFIED`` mean "this build looked and found no proof" instead of
    # "this field happens to be absent".
    #
    # RETIRED ROWS ARE STAMPED TOO. They are dropped from the ACTIVE catalogue a
    # few lines below, but the audit view returns the same objects, and a
    # half-built row there would make the history of a question look poorer than
    # the question ever was.
    for row in all_questions:
        _attach_business_rule(row, rules_by_field)
        row.setdefault("locator_state", locator_state_of(row))
        # A questionnaire question folded in from BRANCH rows has no page-side
        # count — its answers ARE the branch rows, one per option walked. Setting
        # the total to what we hold says "this is the whole set as far as the
        # crawl saw it" rather than leaving a key absent on some rows and present
        # on others, which is the kind of inconsistency a consumer reads as a bug.
        if "options_total" not in row:
            row["options_total"] = len(row.get("options") or [])
        # M2.1 - IS THIS THE APPLICATION'S WORDING, OR NOBODY'S?
        #
        # The catalogue used to publish "Question 1" ... "Question 20" for a
        # bare-button questionnaire: text no element on the page has ever
        # contained, and indistinguishable in the artifact from a question the
        # application really asks. The wording is now captured from the DOM or
        # not at all, and this says which - so a reader can act on the ones that
        # are evidence and chase the ones that are a gap in the application.
        # Identity never depends on it: an UNVERIFIED question is still stably
        # identified, still answerable, still diffable.
        row["name_status"] = (
            QUESTION_NAME_OBSERVED if str(row.get("name") or "").strip()
            else QUESTION_NAME_UNVERIFIED)
        row.setdefault("name_source", "")
        # WHERE THE DEPENDENCY CAME FROM, on every row including the ones with
        # no dependency at all. Set here so the field is never absent on some
        # rows and present on others - a consumer reads that inconsistency as a
        # bug, and it is the same reason ``options_total`` is filled above.
        # ``apply_reveal_dependencies`` upgrades this to ``proven_reveal`` on
        # the rows a branch walk actually proved.
        row["depends_on_source"] = (
            DEPENDS_ON_DECLARED if str(row.get("depends_on") or "").strip()
            else "")
        row.setdefault("revealed_by", [])
        row.setdefault("revealed_by_total", 0)
        # M2.3 — collapse this question's per-sighting lifecycle tallies into the
        # one verdict a reader acts on.
        _resolve_question_lifecycle(row)

    # THE TRIGGER->CHILD JOIN, over EVERY question including the retired ones.
    # A retired question that was revealed by another is still a question whose
    # history a reader may ask about, and the audit view returns these same
    # objects — stamping only the active ones would make the record poorer than
    # the evidence. Runs after the loop above because it needs every row's
    # ``name`` resolved before it can name a trigger.
    if reveal_rules_fn is not None:
        try:
            apply_reveal_dependencies(all_questions, reveal_rules_fn(all_questions))
        except Exception:                     # pragma: no cover - see below
            # A CATALOGUE IS NOT LOST TO A DEPENDENCY. Every question, its
            # wording, its answers, its lifecycle and its business rules are
            # already assembled; the reveal join is the last, additive step. A
            # caller whose rule resolution raises gets the catalogue it would
            # have got before this existed, with ``depends_on_source`` empty and
            # ``revealed_by`` empty — which reads as "nothing was proven", and
            # nothing was.
            logger.warning("catalog.reveal_join_failed", exc_info=True)

    retired = [q for q in all_questions if q["lifecycle"] == LIFECYCLE_RETIRED]
    active = [q for q in all_questions if q["lifecycle"] != LIFECYCLE_RETIRED]
    questions = all_questions if include_retired else active

    summary = {
        "question_count": len(questions),
        "required_count": sum(1 for q in questions if q.get("required")),
        "with_options": sum(1 for q in questions if q.get("options")),
        "pages": sorted({p for q in questions for p in q.get("pages", [])}),
        # ── M2.2 · WHAT THE CATALOGUE ACTUALLY KNOWS ────────────────────────
        # A reviewer's first question about an evidence artifact is how much of
        # it is evidence. These are the counts that answer it, and each is a
        # count of questions carrying PROOF, never of questions we described.
        "with_business_rule": sum(
            1 for q in questions
            if q.get("business_rule_state") == RULE_STATE_OBSERVED),
        "with_dependency": sum(1 for q in questions if q.get("depends_on")),
        # THE TWO KINDS OF DEPENDENCY, COUNTED APART. One is the application's
        # own claim, the other is what a walk proved by answering the trigger and
        # watching the child appear. A single number over both would let a form
        # that declares everything and a questionnaire that declares nothing
        # report the same coverage.
        "with_declared_dependency": sum(
            1 for q in questions
            if q.get("depends_on_source") == DEPENDS_ON_DECLARED),
        "with_proven_dependency": sum(
            1 for q in questions
            if q.get("depends_on_source") == DEPENDS_ON_PROVEN),
        "revealed_by_a_trigger": sum(
            1 for q in questions if q.get("revealed_by")),
        "with_verified_locator": sum(
            1 for q in questions
            if q.get("locator_state") == LOCATOR_STATE_VERIFIED),
        "with_validation": sum(1 for q in questions if q.get("validation")),
        # Questions the application states in its own words, and the ones it
        # states nowhere. Counted rather than hidden: "the application gives this
        # question no text" is a finding a client can act on, and the number that
        # used to stand here was 100% because every unworded question had been
        # given a made-up name.
        "with_observed_name": sum(
            1 for q in questions
            if q.get("name_status") == QUESTION_NAME_OBSERVED),
        "name_unverified": sum(
            1 for q in questions
            if q.get("name_status") == QUESTION_NAME_UNVERIFIED),
        # Questions whose stored answers are a PREFIX of what the page offers.
        # Named as a shortfall on purpose: a catalogue that reports 4 clipped
        # enumerations is more useful than one that reports none because it
        # never counted.
        "options_clipped": sum(
            1 for q in questions
            if int(q.get("options_total") or 0) > len(q.get("options") or [])),
        # ── M2.3 · THE LIFECYCLE ROLL-UP ───────────────────────────────────
        # Reported on BOTH views. A reader of the active catalogue must be able
        # to see that questions are being withheld from it: "0 retired" and an
        # omitted field are different claims, and only one of them is a report.
        "active_count": len(active),
        "stale_count": sum(1 for q in active if q["lifecycle"] == LIFECYCLE_STALE),
        "retired_count": len(retired),
        "includes_retired": bool(include_retired),
    }
    return {"questions": questions, "summary": summary}


def _resolve_question_lifecycle(row: dict[str, Any]) -> None:
    """Collapse a question's per-sighting tallies into ONE lifecycle verdict.

    A question lives on as many pages as the application asks it on, and those
    sightings can disagree — dropped from the application form, still on the
    quote form. The rule is the only one that cannot mislead a reader: the
    question is retired when the LAST place that asked it stopped asking, and
    until then it is active, however many pages it has lost.

    ``retired_at`` / ``retired_in_crawl`` therefore come from the sighting that
    retired LAST — the moment the application stopped asking it anywhere, which
    is the date an auditor is asking about. A question with no sightings at all
    (branch-only rows a build passed no branches for) is left ACTIVE: this
    function reports what the sightings said, and no sightings is not a report.
    """
    seen = int(row.pop("_seen", 0) or 0)
    live = int(row.pop("_live", 0) or 0)
    retirements = row.pop("_retirements", []) or []
    # HOW MANY CRAWLS ACTUALLY LOOKED AND DID NOT FIND IT — the evidence trail,
    # carried up from the sighting that has seen the most misses. Counted per
    # sighting rather than per fold on purpose: a fold that never visited the
    # page did not miss the question, and a column that says "missed 7 crawls"
    # when two of them looked is not evidence, it is an inflated number an
    # auditor would be right to distrust.
    missed = int(row.pop("_missed", 0) or 0)
    row["missed_crawls"] = 0 if live else missed
    if live or not seen:
        row["lifecycle"] = LIFECYCLE_ACTIVE
        row["stale"] = False
        return
    if retirements and len(retirements) == seen:
        last = max(retirements, key=lambda r: str(r.get("retired_at") or ""))
        row["lifecycle"] = LIFECYCLE_RETIRED
        row["stale"] = True
        row["retired_at"] = last.get("retired_at") or ""
        row["retired_in_crawl"] = last.get("retired_in_crawl") or ""
        row["retire_reason"] = last.get("retire_reason") or ""
        if last.get("last_seen_crawl"):
            row["last_seen_crawl"] = last["last_seen_crawl"]
        return
    # Missed everywhere, but nowhere conclusively enough to retire — see
    # :func:`crawl_evidence`. Still catalogued, still planned against, flagged.
    row["lifecycle"] = LIFECYCLE_STALE
    row["stale"] = True


def snapshot_catalog(master: Mapping[str, Any], artifact_id: str = "") -> dict[str, Any]:
    """A deterministic, hashable snapshot of a Master Catalog for versioning (P2)
    and regression diffing (P6).

    The hash is over each question's SHAPE — id, name, type, required, options,
    validation, business rule, expected next page — so a re-crawl that changed a
    question (new option, moved branch, tightened validation) produces a different
    hash, while a re-crawl that changed nothing reproduces it exactly.
    """
    questions = list(master.get("questions") or [])
    canon = json.dumps(
        [[q.get("question_id"), q.get("name"),
          q.get("answer_type") or q.get("type") or "text",
          bool(q.get("required")), sorted(str(o) for o in (q.get("options") or [])),
          q.get("validation") or {}, q.get("business_rule") or "",
          q.get("expected_next_page") or "",
          # M2.2 — a question that became conditional, and a question whose
          # answer set grew behind a clip, are both CHANGES to the application,
          # and a regression diff that cannot see them reports "no change" on a
          # release that altered the form. Included here rather than in a
          # separate marker so the existing P6 diff picks them up unchanged.
          str(q.get("depends_on") or ""), int(q.get("options_total") or 0),
          ] for q in questions],
        sort_keys=True, separators=(",", ":"))
    return {
        "artifact_id": artifact_id,
        "question_count": len(questions),
        "snapshot_hash": hashlib.sha256(canon.encode("utf-8")).hexdigest()[:32],
        "questions": questions,
    }


def extract_outcomes(
    page_state: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Extract displayed outcome values from a page state."""
    if not isinstance(page_state, Mapping):
        return []
    displayed = page_state.get("displayed_values")
    if not isinstance(displayed, list):
        return []

    outcomes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for dv in displayed:
        if not isinstance(dv, Mapping):
            continue
        label = str(dv.get("label") or "").strip()
        if not label:
            continue
        key = _normalize_name(label)
        if key in seen:
            continue
        seen.add(key)

        entry: dict[str, Any] = {"label": label}
        selector = str(dv.get("selector") or "").strip()
        if selector:
            entry["selector"] = selector
        vt = str(dv.get("value_type") or "").strip()
        if vt:
            entry["value_type"] = vt
        outcomes.append(entry)

    return outcomes


def merge_controls(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge incoming controls into an existing inventory.

    Keyed by ``question_id`` where the control has one, else by normalized name.
    Incoming entries update existing ones (latest observation wins), new entries
    are appended.  Capped at 200 to prevent unbounded growth.

    WHY NOT NAME ALONE (M2.1). The name of a question is the weakest thing about
    it: a declared choice group whose page states no wording has NO name, and
    keying on name dropped it from the inventory entirely — the one class of
    question that most needed to be recorded was the one class this function
    silently discarded. A reworded question changed key and appeared twice.
    ``question_id`` is derived from the DOM's own grouping/signature, so it
    survives both. Name remains the key for anything that has no id, which is
    exactly what it always was.
    """
    def _key(ctrl: Mapping[str, Any]) -> str:
        qid = str(ctrl.get("question_id") or "").strip()
        return ("id:" + qid) if qid else "nm:" + _normalize_name(
            str(ctrl.get("name") or ""))

    by_key: dict[str, dict[str, Any]] = {}
    for ctrl in (existing or []):
        if isinstance(ctrl, Mapping):
            key = _key(ctrl)
            if key not in ("nm:", "id:"):
                by_key[key] = dict(ctrl)

    for ctrl in incoming:
        if not isinstance(ctrl, Mapping):
            continue
        key = _key(ctrl)
        if key in ("nm:", "id:"):
            continue
        if key in by_key:
            prev = by_key[key]
            prev.update({k: v for k, v in ctrl.items() if v})
        else:
            by_key[key] = dict(ctrl)

    return list(by_key.values())[:200]


def merge_outcomes(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge incoming outcome displays into existing, keyed by label."""
    by_label: dict[str, dict[str, Any]] = {}
    for out in (existing or []):
        if isinstance(out, Mapping):
            label = str(out.get("label") or "").strip()
            if label:
                by_label[_normalize_name(label)] = dict(out)

    for out in incoming:
        if not isinstance(out, Mapping):
            continue
        label = str(out.get("label") or "").strip()
        if not label:
            continue
        norm = _normalize_name(label)
        if norm in by_label:
            prev = by_label[norm]
            prev.update({k: v for k, v in out.items() if v})
        else:
            by_label[norm] = dict(out)

    return list(by_label.values())[:100]


def build_states_index(
    coverage: Mapping[str, Any] | None,
) -> dict[str, Mapping[str, Any]]:
    """Build a {fingerprint: page_state} lookup from coverage.states."""
    if not isinstance(coverage, Mapping):
        return {}
    states = coverage.get("states")
    if not isinstance(states, list):
        return {}
    index: dict[str, Mapping[str, Any]] = {}
    for state in states:
        if not isinstance(state, Mapping):
            continue
        fp = str(state.get("ax_fingerprint") or "").strip()
        if fp:
            index[fp] = state
    return index


def build_ledger_by_url(
    coverage: Mapping[str, Any] | None,
) -> dict[str, list[Mapping[str, Any]]]:
    """Build a {url: [ledger_entries]} lookup from coverage.field_ledger."""
    if not isinstance(coverage, Mapping):
        return {}
    ledger = coverage.get("field_ledger")
    if not isinstance(ledger, list):
        return {}
    by_url: dict[str, list[Mapping[str, Any]]] = {}
    for entry in ledger:
        if not isinstance(entry, Mapping):
            continue
        url = str(entry.get("url") or "").strip()
        if url:
            by_url.setdefault(url, []).append(entry)
    return by_url


def effective_provenance(
    baseline_status: str,
    rule_fields: set[str] | frozenset[str] | None = None,
) -> str:
    """The provenance badge for controls at a node, given the journey state.

    Returns the HIGHEST applicable provenance — used as the default for
    controls not individually overridden.  Individual controls that match
    a client-declared rule get ``client_declared`` regardless.
    """
    if baseline_status in _CONFIRMED_STATUSES:
        return PROVENANCE_CONFIRMED
    return PROVENANCE_OBSERVED


def apply_provenance(
    controls: list[dict[str, Any]],
    baseline_status: str,
    rule_fields: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Stamp each control with its effective provenance badge.

    Mutates in place and returns the list for convenience.

    * A control whose name matches a client-authored rule field gets
      ``client_declared`` — the client explicitly declares it.
    * Otherwise, if the baseline is approved/validated → ``confirmed``.
    * Otherwise → ``observed``.
    """
    base = effective_provenance(baseline_status)
    norm_rules = frozenset(_normalize_name(f) for f in (rule_fields or []))
    for ctrl in controls:
        name = _normalize_name(str(ctrl.get("name") or ""))
        if name and name in norm_rules:
            ctrl["provenance"] = PROVENANCE_CLIENT_DECLARED
        else:
            ctrl["provenance"] = base
    return controls


def apply_outcome_provenance(
    outcomes: list[dict[str, Any]],
    baseline_status: str,
    rule_fields: set[str] | frozenset[str] | None = None,
) -> list[dict[str, Any]]:
    """Stamp each outcome with its effective provenance badge."""
    base = effective_provenance(baseline_status)
    norm_rules = frozenset(_normalize_name(f) for f in (rule_fields or []))
    for out in outcomes:
        label = _normalize_name(str(out.get("label") or ""))
        if label and label in norm_rules:
            out["provenance"] = PROVENANCE_CLIENT_DECLARED
        else:
            out["provenance"] = base
    return outcomes


def catalog_summary(
    nodes: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute summary statistics for a catalog view."""
    total_controls = 0
    total_outcomes = 0
    controls_with_options = 0
    required_count = 0
    node_count = len(nodes)

    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        for ctrl in (node.get("controls") or []):
            if not isinstance(ctrl, Mapping):
                continue
            total_controls += 1
            if ctrl.get("options"):
                controls_with_options += 1
            if ctrl.get("required"):
                required_count += 1
        total_outcomes += len(node.get("displayed_outcomes") or [])

    return {
        "node_count": node_count,
        "total_controls": total_controls,
        "controls_with_options": controls_with_options,
        "required_controls": required_count,
        "total_outcomes": total_outcomes,
    }
