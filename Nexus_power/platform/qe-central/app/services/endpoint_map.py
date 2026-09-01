"""M2.4 / T-GEN-03 — the endpoint map, joined to the UI step that caused it.

WHAT WAS MISSING, AND WHY IT MATTERED.  The crawl has captured XHR/fetch traffic
per page state for a long time, and every consumer of it was a post-mortem one:
the substrate stores the list, the failure attributor scans it after a run has
already gone red.  Nothing joined a captured call to the CONTROL whose click made
it, so a generated specification could assert that a page rendered and nothing
about the system behind it.  That is the exact shape of the regression this
milestone exists to catch: an API that starts returning the wrong thing behind a
UI that still paints, which every UI-only assertion passes straight through.

TWO ATTRIBUTION RULES, IN PRECEDENCE ORDER.

1. **RECORDED causality (M2.5 / T-NET-03).**  The crawl now stamps every network
   event with the UI action that was in flight when the request went out, and
   ``endpoint_inventory`` carries those forward per endpoint as
   ``actions: [{verb, label, action_token}]``.  When a journey edge's advance
   trigger matches one of those labels, the attribution is not inferred at all —
   it is read.  This is the join T-GEN-03 asks for and it is always preferred.

2. **STRUCTURAL inference, for evidence that predates the stamp.**  The crawl
   drains its network buffer when it RECORDS a state, so the calls a click on
   page A caused are the ones that appear in page B's map and were not already
   in page A's::

       caused(A --click--> B)  =  endpoints(B) \\ endpoints(A)

   Set difference, not "everything at B": B's own page-load calls are already in
   B's map from any other arrival at B, and claiming them for this click would
   attribute a page's boot traffic to whatever control happened to reach it.
   The entry step is the one exception — nothing precedes it, so it owns the
   entry state's map outright.

Every attributed record says WHICH rule produced it (``attribution``), because a
read fact and an inferred one are not the same fact and a generated spec that
cannot tell them apart is a spec nobody can audit.

DETERMINISM IS A REQUIREMENT, NOT A STYLE CHOICE.  T-GEN-02 requires the ranking
over the same evidence to be identical every time, and a compiled spec is a
regression baseline that must not churn.  Every list this module returns is
sorted; nothing here reads a clock, a random source, or a dict insertion order.

Pure and dependency-free: plain mappings in, plain dicts out, tolerant of
malformed input (a bad entry is dropped, never raised).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

#: The keys an endpoint record carries across every boundary in this pipeline.
ENDPOINT_KEYS = ("method", "path", "status", "response_mime")

#: HTTP methods that CHANGE something.  A journey's proof is that its commit
#: actually reached the backend, so these outrank a read when only a bounded
#: number of assertions can ride on one step.
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Per-step ceiling.  A page that fires a dozen analytics-shaped GETs would
#: otherwise compile into a dozen assertions, each of them a future flake for a
#: call the journey never depended on.  Mutations are kept first.
MAX_ASSERTIONS_PER_STEP = 3


def normalize_path(url_or_path: str) -> str:
    """One normalisation for every endpoint comparison: the PATH component,
    lower-cased, trailing slash stripped (bare '/' preserved).

    Deliberately the same shape as ``journey_case_linker.norm_path``: a spec
    generated against one environment has to run against another, and a host- or
    query-pinned endpoint assertion is a spec that only ever runs once.
    """
    raw = str(url_or_path or "").strip()
    path = urlsplit(raw).path if "://" in raw else raw
    path = (path or "").split("?", 1)[0].split("#", 1)[0].strip().lower()
    if not path:
        return ""
    return path.rstrip("/") or "/"


def normalize_endpoint(entry: Any) -> dict[str, str] | None:
    """One captured call to the canonical endpoint record, or ``None``.

    Accepts both shapes this pipeline carries — the explorer's already-reduced
    ``{method, path, status}`` and a raw page-state ``{method, url, status}`` —
    so a caller never has to know which side of the boundary it is holding.
    """
    if not isinstance(entry, Mapping):
        return None
    method = str(entry.get("method") or "").strip().upper()[:10]
    path = normalize_path(entry.get("path") or entry.get("url") or "")
    if not method or not path:
        return None
    try:
        status = int(str(entry.get("status") or "").strip())
    except ValueError:
        return None
    # Only a settled success is a claim a generated spec may hold an application
    # to.  A 3xx is a hop, a 0 never settled, and a 4xx/5xx the crawl happened
    # to observe is a DEFECT — compiling it in would freeze the application's
    # bug into the regression suite as its expected behaviour.
    if not 200 <= status < 300:
        return None
    return {
        "method": method,
        "path": path,
        "status": str(status),
        "response_mime": str(entry.get("response_mime") or "").strip()[:100],
    }


def endpoints_of(node: Any) -> list[dict[str, str]]:
    """The canonical, sorted endpoint map of one journey node.

    Reads ``observed_endpoints`` (what the fold persists) and falls back to
    ``endpoints`` (what ``coverage.states`` carries), so the same function serves
    a database row and a freshly-folded state without a caller-side branch.
    """
    if node is None:
        return []
    if isinstance(node, Mapping):
        raw = node.get("observed_endpoints") or node.get("endpoints")
    else:
        raw = getattr(node, "observed_endpoints", None) or getattr(
            node, "endpoints", None)
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for entry in raw:
        record = normalize_endpoint(entry)
        if record is not None:
            by_key.setdefault((record["method"], record["path"]), record)
    return [by_key[key] for key in sorted(by_key)]


def _key(endpoint: Mapping[str, str]) -> tuple[str, str]:
    return (str(endpoint.get("method") or ""), str(endpoint.get("path") or ""))


# ── Rule 1 · the RECORDED causal join (M2.5 / T-NET-03) ────────────────────

#: How an attributed endpoint says where its attribution came from.
ATTRIBUTION_RECORDED = "recorded"      # the crawl stamped the action on the event
ATTRIBUTION_INFERRED = "inferred"      # derived from the state-to-state difference


def normalize_action_label(text: str) -> str:
    """The one label normalisation shared with the journey graph.

    Identical to ``journey_case_linker.normalize_option_label`` — an edge's
    ``trigger_label_norm`` was produced by that function, and a join across two
    different normalisations is a join that silently misses.
    """
    return " ".join(str(text or "").split()).strip().lower()[:200]


def inventory_by_action(inventory: Any) -> dict[str, list[dict[str, str]]]:
    """``{normalized UI action label: [endpoint, ...]}`` from an M2.5 inventory.

    The inventory's rows are application-level (``method x path_template``) and
    each carries the UI actions the crawl OBSERVED firing it.  Inverting that
    mapping is the whole join: a journey edge knows the label it clicked, and
    this says which endpoints that click was seen to call.

    Only settled 2xx endpoints cross, for the reason stated on
    :func:`normalize_endpoint`: an inventory row is an honest record of every
    status including the failures, and a COMPILER must not turn an observed 5xx
    into the behaviour a regression suite demands.
    """
    out: dict[str, list[dict[str, str]]] = {}
    rows = inventory
    if isinstance(inventory, Mapping):
        rows = inventory.get("endpoints") or inventory.get("endpoint_inventory")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        method = str(row.get("method") or "").strip().upper()[:10]
        path = normalize_path(row.get("path_template") or row.get("path") or "")
        if not method or not path:
            continue
        statuses = row.get("statuses")
        status = _best_status(statuses)
        if status is None:
            continue
        record = {
            "method": method,
            "path": path,
            "status": str(status),
            "response_mime": str(row.get("response_shape") or "").strip()[:100],
            "attribution": ATTRIBUTION_RECORDED,
        }
        for action in (row.get("actions") or ()):
            if not isinstance(action, Mapping):
                continue
            label = normalize_action_label(action.get("label"))
            if not label:
                continue
            bucket = out.setdefault(label, [])
            if not any(_key(e) == _key(record) for e in bucket):
                bucket.append(record)
    for label in out:
        out[label] = sorted(out[label], key=_key)
    return out


def navigate_caused(inventory: Any) -> list[dict[str, str]]:
    """The endpoints the crawl recorded a NAVIGATION firing, sorted.

    A2.2 — WHY THE ENTRY STEP NEEDED ITS OWN JOIN. ``inventory_by_action`` indexes
    on the clicked LABEL and skips actions that have none, which is right for its
    callers and leaves a page LOAD unreachable through it: a navigation has a verb
    and no label. So the entry step of a compiled journey had no recorded source
    at all and fell back to inference over the entry state's endpoint map.

    That map is the whole set of calls DRAINED during the visit, which includes
    anything an on-page action fired. Measured on the M2.4 quote funnel: the entry
    state carried BOTH ``GET /api/config`` (its page load) and ``POST /api/quote``
    (fired by a discovery click that then navigated away), so the compiled step 1
    asserted a POST that opening the page does not make — and the specification
    went RED against a healthy application. A false regression is the exact mirror
    of a green-wash, and it is worse in one respect: it teaches an operator to
    ignore the suite.

    The inventory already holds the correct answer and nothing was reading it —
    ``/api/config`` carries a ``navigate`` action and ``/api/quote`` carries a
    ``click`` one. This reads that, so the entry step asserts what a page LOAD was
    observed to call, as RECORDED evidence rather than as a guess.

    Same 2xx-only rule as :func:`inventory_by_action`, and for the same reason: a
    compiler must not turn an observed failure into required behaviour.
    """
    rows = inventory
    if isinstance(inventory, Mapping):
        rows = inventory.get("endpoints") or inventory.get("endpoint_inventory")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return []
    out: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        method = str(row.get("method") or "").strip().upper()[:10]
        path = normalize_path(row.get("path_template") or row.get("path") or "")
        if not method or not path:
            continue
        status = _best_status(row.get("statuses"))
        if status is None:
            continue
        verbs = {
            str(a.get("verb") or "").strip().lower()
            for a in (row.get("actions") or ()) if isinstance(a, Mapping)
        }
        if "navigate" not in verbs:
            continue
        record = {
            "method": method,
            "path": path,
            "status": str(status),
            "response_mime": str(row.get("response_shape") or "").strip()[:100],
            "attribution": ATTRIBUTION_RECORDED,
        }
        if not any(_key(e) == _key(record) for e in out):
            out.append(record)
    return sorted(out, key=_key)


def by_action_from_edges(edges: Any) -> dict[str, list[dict[str, str]]]:
    """``{trigger label: [endpoint, ...]}`` read from JOURNEY EDGE ROWS.

    The same index as :func:`inventory_by_action`, sourced from what the fold
    PERSISTED rather than from a crawl-time blob.  Both exist because the two
    callers hold different things: a fold has the whole coverage payload in
    hand, while an API request has only the graph rows — and if the request path
    could not read the persisted join it would silently fall back to structural
    inference on every call, turning a recorded fact into an inferred one for no
    reason a reader could see.
    """
    out: dict[str, list[dict[str, str]]] = {}
    for edge in edges or ():
        if isinstance(edge, Mapping):
            label = edge.get("trigger_label_norm")
            raw = edge.get("observed_endpoints")
        else:
            label = getattr(edge, "trigger_label_norm", "")
            raw = getattr(edge, "observed_endpoints", None)
        key = normalize_action_label(label)
        if not key or not raw:
            continue
        bucket = out.setdefault(key, [])
        for entry in raw:
            record = normalize_endpoint(entry)
            if record is None:
                continue
            record["attribution"] = ATTRIBUTION_RECORDED
            if not any(_key(e) == _key(record) for e in bucket):
                bucket.append(record)
    for key in list(out):
        if out[key]:
            out[key] = sorted(out[key], key=_key)
        else:
            del out[key]
    return out


def _best_status(statuses: Any) -> int | None:
    """The 2xx an inventory row settled on, or ``None``.

    An endpoint the crawl saw retry — ``{"503": 2, "200": 1}`` — DID eventually
    succeed, and the success is the behaviour a regression test should demand.
    An endpoint that only ever failed has no successful status and contributes
    nothing to a compiled assertion, which is the correct outcome: a test must
    not be generated that requires an application's bug.
    """
    if not isinstance(statuses, Mapping):
        return None
    best: int | None = None
    for raw, count in statuses.items():
        try:
            status, seen = int(str(raw)), int(count)
        except (TypeError, ValueError):
            continue
        if seen <= 0 or not 200 <= status < 300:
            continue
        if best is None or status < best:
            best = status
    return best


def merge_endpoints(existing: Any, fresh: Sequence[Mapping[str, str]]) -> list[dict[str, str]]:
    """Union two endpoint maps for the same subject, first-observation-wins.

    Merged rather than replaced, and first-wins rather than last-wins, for the
    same reason the control inventory and the displayed outcomes are: a re-crawl
    that happens not to exercise a call must not erase the evidence that the call
    exists, and a later sighting must not silently rewrite the status a spec was
    already generated against.  Sorted, so two folds of one crawl produce
    byte-identical rows.
    """
    by_key: dict[tuple[str, str], dict[str, str]] = {}
    for source in (existing or (), fresh or ()):
        if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
            continue
        for entry in source:
            record = normalize_endpoint(entry)
            if record is not None:
                # ``attribution`` rides through when the caller set one: a
                # merge must not launder a recorded cause into an inferred one.
                if isinstance(entry, Mapping) and entry.get("attribution"):
                    record["attribution"] = str(entry["attribution"])
                by_key.setdefault((record["method"], record["path"]), record)
    return [by_key[key] for key in sorted(by_key)]


def caused_by(
    source_node: Any, dest_node: Any, *, is_entry: bool = False,
) -> list[dict[str, str]]:
    """The endpoints ONE UI step caused: ``endpoints(dest) \\ endpoints(source)``.

    ``is_entry`` marks the step nothing precedes (the opening navigation): it
    owns the destination's whole map, because there is no earlier state whose
    boot traffic could be confused with it.
    """
    dest = endpoints_of(dest_node)
    if is_entry:
        return dest
    already = {_key(e) for e in endpoints_of(source_node)}
    return [e for e in dest if _key(e) not in already]


def rank_for_assertion(
    endpoints: Sequence[Mapping[str, str]],
    *, limit: int = MAX_ASSERTIONS_PER_STEP,
) -> list[dict[str, str]]:
    """The endpoints worth compiling into assertions, most load-bearing first.

    A mutation outranks a read: the whole claim of an end-to-end journey is that
    its commit REACHED the backend, and when only a bounded number of assertions
    may ride on one step, those are the ones that must.  Ties break on
    (method, path) so the output is byte-stable across two folds of one crawl.
    """
    ordered = sorted(
        (dict(e) for e in endpoints or ()),
        key=lambda e: (
            0 if str(e.get("method") or "").upper() in MUTATING_METHODS else 1,
            str(e.get("method") or ""),
            str(e.get("path") or ""),
        ),
    )
    return ordered[: max(0, int(limit))]


def attribute_steps(
    path_fps: Sequence[str], nodes_by_fp: Mapping[str, Any],
    *,
    step_labels: Sequence[str] = (),
    by_action: Mapping[str, Sequence[Mapping[str, str]]] | None = None,
) -> list[dict[str, Any]]:
    """The endpoint map joined to every UI step of one walked path.

    Returns one record per node in the path::

        {"fingerprint", "is_entry", "label", "attribution",
         "caused": [...], "assertable": [...]}

    ``step_labels[i]`` is the advance trigger that LEFT ``path_fps[i]`` (so the
    terminal state has none), and ``by_action`` is
    :func:`inventory_by_action` over the crawl's M2.5 endpoint inventory.  When
    both are supplied and the label is present, the endpoints are READ from the
    recorded causality; otherwise they are inferred from the state difference.
    Each record names which happened, and never silently mixes the two.

    ``caused`` is the honest full attribution; ``assertable`` is the bounded,
    mutation-first subset a compiler should emit.  Both are sorted.
    """
    labels = [normalize_action_label(l) for l in (step_labels or ())]
    action_index = dict(by_action or {})
    out: list[dict[str, Any]] = []
    previous: Any = None
    for index, fp in enumerate(str(f) for f in (path_fps or ())):
        node = nodes_by_fp.get(fp) if isinstance(nodes_by_fp, Mapping) else None
        # The endpoints belonging to the step that ARRIVES at this state are the
        # ones the previous state's advance fired, so the label read here is the
        # one that left the PREVIOUS node.
        label = labels[index - 1] if 0 < index <= len(labels) else ""
        recorded = [dict(e) for e in (action_index.get(label) or ())] if label else []
        if recorded:
            caused, attribution = recorded, ATTRIBUTION_RECORDED
        else:
            caused = [
                {**e, "attribution": ATTRIBUTION_INFERRED}
                for e in caused_by(previous, node, is_entry=(index == 0))
            ]
            attribution = ATTRIBUTION_INFERRED
        out.append({
            "fingerprint": fp,
            "is_entry": index == 0,
            "label": label,
            "attribution": attribution,
            "caused": caused,
            "assertable": rank_for_assertion(caused),
        })
        previous = node
    return out


__all__ = [
    "ENDPOINT_KEYS",
    "MUTATING_METHODS",
    "MAX_ASSERTIONS_PER_STEP",
    "ATTRIBUTION_RECORDED",
    "ATTRIBUTION_INFERRED",
    "normalize_action_label",
    "inventory_by_action",
    "by_action_from_edges",
    "normalize_path",
    "normalize_endpoint",
    "endpoints_of",
    "merge_endpoints",
    "caused_by",
    "rank_for_assertion",
    "attribute_steps",
]
