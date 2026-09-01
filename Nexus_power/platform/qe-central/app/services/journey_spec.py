"""M2.4 / T-GEN-01 — a discovered journey compiles on its own evidence.

WHY A JOURNEY COULD NOT BE RUN, stated exactly.  ``journey_case_linker`` does not
compile anything.  It MATCHES: the factory generates cases from the whole crawl
artifact, the linker scores each one against a journey's walked pages and advance
labels, and the highest-scoring case that spans the funnel end to end is ADOPTED
as that journey's runnable form.  Everything downstream — ``_runnable_view``,
``POST .../run``, the Top-N an operator would want — hangs off that adoption.

So a journey's runnability was never a property of the journey.  It was a
property of whether some OTHER artifact-level case happened to walk the same
pages in the same order, and the failure modes were all silent-by-design:

  * no case spans it            → "no test case walks this journey's pages in
                                  order on the current crawl artifact";
  * the artifact regenerated    → the adoption is re-derived and can vanish;
  * a case spans it and wanders → the tightest-fit tie-break exists precisely
                                  because an adopted case can assert things the
                                  journey never claimed.

Every one of those is a correct implementation of the wrong primitive.  A walked
journey already holds everything a runnable specification needs — the entry URL,
the ordered states, the control that advanced each one, the endpoints those
advances called, and the outcome values the funnel produced.  This module reads
exactly that and emits a compile payload.  Adoption stays where it is and keeps
doing its job (re-using a human-owned case when one really does span the
journey); it is no longer the ONLY door.

WHAT IS AND IS NOT GROUNDED.  Every step here comes from a COMPLETED traversal —
the walk the crawl finished, not the branches it merely saw.  The advance labels
are the edges it actually clicked.  The endpoints are the calls those clicks
actually caused (``endpoint_map``).  The value assertions are the outcome values
the funnel actually displayed, bound to the selector the crawl actually captured.
Nothing is inferred, and a journey with no completed traversal compiles to
nothing at all with a named reason — never to a spec that pretends.

Pure and dependency-free (plain mappings in, plain dict out) so the whole
generation contract is unit-testable without a database, a browser or a network.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from . import endpoint_map

#: Baseline states in which a journey's outcome criterion is CONFIRMED — a human
#: approved this baseline, or a later walk validated it.  T-GEN-04 turns exactly
#: this into a hard oracle; everything else stays honestly soft.
CONFIRMED_BASELINE_STATUSES = frozenset({"approved", "validated"})

#: Value types whose displayed outcome is a NUMBER, and therefore assertable with
#: a numeric comparison and a tolerance rather than a text match.
_NUMERIC_VALUE_TYPES = frozenset({"currency", "number", "percent", "decimal"})

#: Absolute tolerance for a numeric outcome assertion.  Zero would make a
#: rounding difference in a renderer a false regression; anything larger would
#: let a real pricing change through.  A cent is the smallest unit any of these
#: applications display.
NUMERIC_TOLERANCE = 0.01

_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _text(value: Any) -> str:
    return _WS_RE.sub(" ", str(value or "").strip())


def _get(row: Any, attr: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(attr, default)
    return getattr(row, attr, default)


def _normalize_label(text: str) -> str:
    """The one label normalisation this pipeline uses (matches
    ``journey_case_linker.normalize_option_label``), so an edge trigger recorded
    as ``trigger_label_norm`` can be matched back to the control that carries the
    original casing."""
    return _WS_RE.sub(" ", str(text or "").strip().lower())[:200]


def test_id_for(tenant_id: str, journey_id: str) -> str:
    """A stable id for the journey's own generated case.

    Keyed on the JOURNEY, not on the artifact: a re-crawl mints a new artifact
    and must not mint a new test identity for the same business path, or every
    history join (verdict timeline, baseline, run ledger) breaks on re-crawl —
    which is the precise weakness of hanging runnability off adoption.
    """
    digest = hashlib.sha256(
        "\x1f".join(("journey_spec", str(tenant_id), str(journey_id))).encode()
    ).hexdigest()
    return f"jny_{digest[:24]}"


def display_name_for(business_name: str, entry_title: str) -> str:
    """The F5 business name — the same sentence adoption puts on a linked case,
    so an operator cannot tell from the name whether a spec came from adoption or
    from direct compilation, only from its provenance field."""
    name = _text(business_name) or _text(entry_title)
    return (f"Verify {name} end to end" if name else "Verify journey end to end")[:300]


def control_names_by_norm(node: Any) -> dict[str, str]:
    """``{normalized name: original name}`` for one node's control inventory.

    Edges store ``trigger_label_norm`` (lower-cased, whitespace-collapsed), and a
    Playwright accessible-name locator wants the name the PAGE renders.  Losing
    the casing would be survivable for a case-insensitive role locator and is not
    survivable for a ``getByLabel``, so the original is recovered here from the
    inventory the same fold wrote.
    """
    out: dict[str, str] = {}
    for control in (_get(node, "controls_inventory") or ()):
        if not isinstance(control, Mapping):
            continue
        name = _text(control.get("name"))
        if name:
            out.setdefault(_normalize_label(name), name)
    return out


def control_kind_by_norm(node: Any) -> dict[str, str]:
    """``{normalized name: control type}`` for one node — lets a compiled step
    say WHAT it is clicking (button, link) instead of guessing from the label."""
    out: dict[str, str] = {}
    for control in (_get(node, "controls_inventory") or ()):
        if not isinstance(control, Mapping):
            continue
        name = _text(control.get("name"))
        if name:
            out.setdefault(_normalize_label(name), _text(control.get("type")).lower())
    return out


def edge_index(edges: Sequence[Any]) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(from_fp, to_fp): edge}`` for the walked path lookup.

    When a pair has been walked by more than one trigger, the HIGHEST advance
    tier wins: tier 3 is an agent oracle's pick and tiers 1/2 are deterministic
    regex matches, so preferring the highest keeps the label that a real
    decision produced rather than whichever row was inserted first.
    """
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in edges or ():
        key = (str(_get(edge, "from_fp") or ""), str(_get(edge, "to_fp") or ""))
        if not key[0] or not key[1]:
            continue
        candidate = {
            "trigger_label_norm": _text(_get(edge, "trigger_label_norm")),
            "advance_tier": int(_get(edge, "advance_tier") or 0),
        }
        current = best.get(key)
        if current is None or candidate["advance_tier"] > current["advance_tier"]:
            best[key] = candidate
    return best


def _numeric(value: str) -> float | None:
    match = _NUM_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def outcome_selectors(nodes: Sequence[Any]) -> dict[str, str]:
    """``{normalized outcome label: selector}`` across a journey's nodes.

    ``displayed_outcomes`` is what the crawl captured for each rendered value
    node — the label it is shown under and the selector that reaches it.  That
    selector is exactly the ``source_hint`` the value oracle needs, which is why
    a journey can carry a HARD outcome assertion at all: the ground is a captured
    DOM node, not a sentence about one.
    """
    out: dict[str, str] = {}
    for node in nodes or ():
        for outcome in (_get(node, "displayed_outcomes") or ()):
            if not isinstance(outcome, Mapping):
                continue
            label = _text(outcome.get("label"))
            selector = _text(outcome.get("selector"))
            if label and selector:
                out.setdefault(_normalize_label(label), selector)
    return out


def value_assertions_for(
    outcome_values: Sequence[Any], selectors: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """The journey's outcome values as value-oracle assertions.

    Returns ``(assertions, ungrounded_labels)``.  An outcome with no captured
    selector is NOT emitted as a text-matching guess against the whole page — it
    is returned in the second list so the caller can say plainly which criteria
    could not be grounded.  A silent drop and a fabricated locator are the two
    ways this becomes green-wash, and both are refused here.
    """
    assertions: list[dict[str, Any]] = []
    ungrounded: list[str] = []
    seen: set[str] = set()
    for entry in outcome_values or ():
        if not isinstance(entry, Mapping):
            continue
        label = _text(entry.get("label"))
        value = _text(entry.get("value"))
        if not label or not value:
            continue
        key = _normalize_label(label)
        if key in seen:
            continue
        seen.add(key)
        selector = selectors.get(key, "")
        if not selector:
            ungrounded.append(label)
            continue
        value_type = _text(entry.get("value_type")).lower()
        number = _numeric(value) if value_type in _NUMERIC_VALUE_TYPES else None
        if number is not None:
            assertions.append({
                "field": label,
                "expected": number,
                "match": "numeric",
                "tolerance": NUMERIC_TOLERANCE,
                "source_hint": selector,
                "observed_text": value,
            })
        else:
            assertions.append({
                "field": label,
                "expected": value,
                "match": "contains",
                "source_hint": selector,
                "observed_text": value,
            })
    return assertions, ungrounded


def is_confirmed(journey: Any, traversal: Any) -> bool:
    """Is this journey's outcome criterion CONFIRMED?

    Both halves are required, and neither is a formality:

      * the walk COMPLETED — an unfinished funnel has no end-state to hold an
        application to, whatever a human approved;
      * the baseline is approved or validated — a human (or a later confirming
        walk) has said that end-state is the correct one.

    Only then does T-GEN-04 promote the outcome oracle to a hard, failing
    assertion.  A merely-captured baseline stays soft, and says so.
    """
    if not bool(_get(traversal, "completed")):
        return False
    return _text(_get(journey, "baseline_status")).lower() in CONFIRMED_BASELINE_STATUSES


def build_journey_case(
    journey: Any,
    *,
    traversal: Any,
    nodes_by_fp: Mapping[str, Any],
    edges: Sequence[Any] = (),
    tenant_id: str = "",
    criticality: Mapping[str, Any] | None = None,
    endpoint_inventory: Any = None,
) -> dict[str, Any]:
    """Compile a discovered journey into a factory compile payload.

    Returns either ``{"compilable": False, "reason": "..."}`` — always with a
    named, actionable reason — or the full payload::

        {compilable, test_id, journey_id, name, description, base_url,
         outcome_oracle, steps: [...], value_assertions: [...],
         ungrounded_outcomes: [...], endpoints_asserted, criticality}

    The steps are the journey's own walk: one navigation to the entry state,
    then one click per edge the traversal crossed, each carrying the endpoints
    that click caused and the page it landed on.
    """
    journey_id = str(_get(journey, "journey_id") or "")
    if traversal is None:
        return {"compilable": False, "journey_id": journey_id, "reason": (
            "no completed walk yet — a journey compiles from the path the crawl "
            "finished, and this one has none. Re-crawl to prove the path.")}
    path_fps = [str(fp) for fp in (_get(traversal, "path_fps") or ()) if str(fp)]
    if len(path_fps) < 2:
        return {"compilable": False, "journey_id": journey_id, "reason": (
            "this walk never advanced past its first state, so there is no "
            "end-to-end path to re-prove — crawl deeper (raise the crawl budget "
            "or scope End-to-end mode at this funnel)")}

    nodes = [nodes_by_fp.get(fp) for fp in path_fps]
    entry_url = _text(_get(journey, "entry_url")) or _text(_get(nodes[0], "url"))
    if not entry_url:
        return {"compilable": False, "journey_id": journey_id, "reason": (
            "the walked entry state carries no URL, so there is nothing to "
            "navigate to — the crawl evidence for this journey is incomplete")}

    by_pair = edge_index(edges)
    # The advance trigger that LEAVES each state, in walk order — the key the
    # M2.5 endpoint inventory is joined on, so a step's network assertion is the
    # call the crawl RECORDED that click making rather than one inferred from
    # which state it landed on.
    step_labels = [
        (by_pair.get((path_fps[i], path_fps[i + 1])) or {}).get(
            "trigger_label_norm", "")
        for i in range(len(path_fps) - 1)
    ]
    # The recorded causal join, from whichever side the caller is holding.
    # A FOLD has the crawl's whole coverage payload and passes the M2.5
    # inventory; an API REQUEST has only graph rows, where the same join lives
    # on ``journey_edges.observed_endpoints``. Reading only the first would make
    # every request-path attribution silently degrade to inference — a recorded
    # fact reported as a guess, which is the failure this milestone is about.
    by_action = endpoint_map.inventory_by_action(endpoint_inventory)
    if not by_action:
        by_action = endpoint_map.by_action_from_edges(edges)
    attribution = endpoint_map.attribute_steps(
        path_fps, nodes_by_fp, step_labels=step_labels, by_action=by_action)
    # A2.2 — the entry step's own recorded join; see the entry step below. Empty
    # for a caller with no inventory, which keeps the old inferred path intact.
    _entry_network = endpoint_map.navigate_caused(endpoint_inventory)

    steps: list[dict[str, Any]] = []
    entry_title = _text(_get(journey, "entry_title")) or _text(_get(nodes[0], "title"))
    steps.append({
        "step_number": 1,
        "action": f"Open {entry_title or 'the journey entry page'}",
        "expected_result": (
            f"The {entry_title} page is shown" if entry_title
            else "The journey entry page is shown"),
        "confidence": "high",
        "provenance": "observed",
        "observed": {
            "verb": "navigate",
            "url": entry_url,
            "label": "",
            "after": entry_title,
        },
        # A2.2 — WHAT A PAGE LOAD WAS RECORDED CALLING, not what the visit
        # drained. The line below used to read the entry state's whole endpoint
        # map, on the reasoning that "nothing precedes it, so no earlier state's
        # boot traffic can be confused with the calls this navigation made".
        # Nothing EARLIER can, but something LATER can: the map is everything
        # drained during the visit, and a discovery click fires calls too.
        # Measured on the M2.4 quote funnel — the entry state carried both
        # GET /api/config (its load) and POST /api/quote (a discovery click that
        # then navigated away), so step 1 asserted a POST that opening the page
        # does not make and the spec went RED on a healthy application.
        #
        # ``navigate_caused`` reads the recorded verb join the M2.5 inventory has
        # always carried and nothing consulted, so this becomes RECORDED evidence
        # rather than a better guess. The inferred path is kept as the fallback
        # for a caller with no inventory (the API request path), unchanged.
        "network_expect": (
            _entry_network or (attribution[0]["assertable"] if attribution else [])),
        "network_attribution": (
            endpoint_map.ATTRIBUTION_RECORDED if _entry_network
            else (attribution[0]["attribution"] if attribution
                  else endpoint_map.ATTRIBUTION_INFERRED)),
    })

    unwalkable: list[str] = []
    for index in range(len(path_fps) - 1):
        from_fp, to_fp = path_fps[index], path_fps[index + 1]
        edge = by_pair.get((from_fp, to_fp))
        if edge is None or not edge["trigger_label_norm"]:
            # The traversal crossed a pair with no named trigger.  A step cannot
            # be fabricated for it — the crawl does not know what was clicked —
            # so the journey is refused rather than compiled with a gap that
            # would silently reorder every later assertion.
            unwalkable.append(f"{from_fp[:12]}->{to_fp[:12]}")
            continue
        source_node = nodes_by_fp.get(from_fp)
        dest_node = nodes_by_fp.get(to_fp)
        norm = edge["trigger_label_norm"]
        label = control_names_by_norm(source_node).get(norm, norm)
        kind = control_kind_by_norm(source_node).get(norm, "")
        next_url = _text(_get(dest_node, "url"))
        next_title = _text(_get(dest_node, "title"))
        steps.append({
            "step_number": index + 2,
            "action": f"Click {label}",
            "expected_result": (
                f"The {next_title} state is shown" if next_title
                else "The next state of the journey is shown"),
            # Tier 1/2 are deterministic advance decisions and tier 3 is an
            # agent's; a tier-0 edge predates the evidence and is honestly
            # marked review so the compiler's own UNPROVEN handling applies.
            "confidence": "high" if edge["advance_tier"] >= 1 else "review",
            "provenance": "observed" if edge["advance_tier"] >= 1 else "inferred",
            "observed": {
                "verb": "click",
                "label": label,
                "kind": kind or "button",
                "url": _text(_get(source_node, "url")),
                "next_url": next_url,
                "after": next_title,
                # THE CAUSAL CLAIM, and the evidence for it. The auditor refuses
                # a ``toHaveURL`` that attributes a page change to a click the
                # recording does not show causing it — correctly, because that
                # is an always-red assertion. A journey edge IS that recording:
                # the traversal crossed from this state to the next one THROUGH
                # this trigger, and the advance tier says who decided it. Tier 0
                # predates the evidence, so it does not get to make the claim.
                "navigation_grounded": edge["advance_tier"] >= 1,
            },
            "network_expect": attribution[index + 1]["assertable"],
            "network_attribution": attribution[index + 1]["attribution"],
        })

    if unwalkable:
        return {"compilable": False, "journey_id": journey_id, "reason": (
            "the walked path crosses "
            f"{len(unwalkable)} transition(s) with no recorded trigger control "
            f"({', '.join(unwalkable[:3])}) — the crawl cannot say what was "
            "clicked, so no step can be compiled for them")}

    selectors = outcome_selectors(nodes)
    assertions, ungrounded = value_assertions_for(
        _get(traversal, "outcome_values") or (), selectors)
    confirmed = is_confirmed(journey, traversal)
    # T-GEN-04, AND ITS CONVERSE.  A CONFIRMED criterion becomes a hard value
    # assertion that can fail the build — that is the whole ticket.  An
    # UNCONFIRMED one is withheld rather than downgraded, and the difference
    # matters in both directions:
    #
    #   * emitting it soft would be the exact defect being closed here, an
    #     informational log wearing an assertion's name;
    #   * emitting it hard would let a value nobody has approved fail somebody's
    #     build the first time the application legitimately changes.
    #
    # So it is reported by name as awaiting approval.  Approving the baseline
    # arms it, which is a decision a human makes on the record.
    unconfirmed = []
    if not confirmed:
        unconfirmed = [a["field"] for a in assertions]
        assertions = []
    business_name = _text(_get(journey, "business_name"))

    endpoints_asserted = sum(len(s.get("network_expect") or []) for s in steps)
    # How many of those assertions rest on a RECORDED cause rather than an
    # inferred one. Reported rather than averaged away: an operator deciding
    # whether to trust a generated network assertion needs to know which kind
    # it is looking at.
    endpoints_recorded = sum(
        len(s.get("network_expect") or []) for s in steps
        if s.get("network_attribution") == endpoint_map.ATTRIBUTION_RECORDED)
    return {
        "compilable": True,
        "journey_id": journey_id,
        "test_id": test_id_for(tenant_id, journey_id),
        "name": display_name_for(business_name, entry_title),
        "description": _text(_get(journey, "name_description")),
        "expected_outcome": _text(_get(traversal, "terminal")),
        "base_url": f"{urlsplit(entry_url).scheme}://{urlsplit(entry_url).netloc}",
        "steps": steps,
        "value_assertions": assertions,
        # Named, not dropped: a criterion the crawl saw but cannot ground is a
        # gap in the evidence and the operator has to be told which one.
        "ungrounded_outcomes": ungrounded,
        # Grounded, but not yet approved — so armed as nothing, and listed so
        # the operator can see exactly what approving the baseline would buy.
        "unconfirmed_outcomes": unconfirmed,
        # T-GEN-04 — a CONFIRMED journey's outcome criterion must be able to
        # fail the test.  Anything else stays soft and says so.
        "outcome_oracle": "hard" if confirmed else "soft",
        "outcome_oracle_reason": (
            "baseline "
            f"{_text(_get(journey, 'baseline_status')).lower() or 'captured'}"
            + (" on a completed walk — outcome criteria are hard assertions"
               if confirmed else
               " — outcome criteria stay non-failing until a baseline is "
               "approved on a completed walk")),
        "endpoints_asserted": endpoints_asserted,
        "endpoints_recorded_cause": endpoints_recorded,
        "traversal_id": str(_get(traversal, "traversal_id") or ""),
        "provenance": "journey_direct",
        "criticality": dict(criticality or {}),
    }


__all__ = [
    "CONFIRMED_BASELINE_STATUSES",
    "NUMERIC_TOLERANCE",
    "test_id_for",
    "display_name_for",
    "control_names_by_norm",
    "control_kind_by_norm",
    "edge_index",
    "outcome_selectors",
    "value_assertions_for",
    "is_confirmed",
    "build_journey_case",
]
