"""M2.4 / T-GEN-02 — criticality bound to a journey, and the ranked Top-N.

``criticality.evaluate`` has been a complete, deterministic, tested classifier
since S4 and NOTHING IN THE JOURNEY SURFACE HAS EVER CALLED IT.  It bands a
*subject* — a projection with url paths, field labels, button labels and action
verbs — and the only projector shipped with it (``subject_from_journey``) reads
the synthesis shape ``{"kind", "pages": [...]}``, which the journey graph does
not speak.  So the registry banded scenario projections and personas while the
journeys themselves, the objects an operator actually chooses between, carried no
band at all: ``GET /apps/{id}/journeys`` returned counts and runnability and no
answer whatsoever to "which of these twenty matters most".

This module is the missing adapter and nothing more.  It projects a journey's OWN
evidence — the graph rows — into the subject vocabulary the registry already
understands, then ranks.  The classifier is not reimplemented, re-tuned, or
wrapped in a second opinion; a band here is a band ``criticality.evaluate``
produced, with its evidence list carried through verbatim so the API can show
WHICH marker fired rather than a number nobody can audit.

DETERMINISM IS PART OF THE CONTRACT.  T-GEN-02 requires the same evidence to
produce the same order every time, so the sort key is total and every component
of it is a stored fact:

  1. band            — P0 before P1 before P2 before P3;
  2. deepest_steps   — a longer funnel is more of the application, descending;
  3. paths_completed — a journey PROVEN end to end outranks one merely walked;
  4. boundary nodes  — a path that crosses a commit outranks one that browses;
  5. endpoints       — more observed backend behaviour is more to regress;
  6. journey_id      — the tie-break of last resort, so two identical journeys
                       never swap places between two reads of one database.

Nothing in that key is a timestamp, a row order, or a name — a rename must not
reorder the list, and neither must a re-fold.

Pure and dependency-free apart from the registry itself: plain mappings in,
plain dicts out.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from . import criticality
from .endpoint_map import endpoints_of

#: The default size of the ranked list the API exposes (T-GEN-02: "Top-20").
TOP_N_DEFAULT = 20

#: Control types that are QUESTIONS (they carry a field label) rather than
#: TARGETS (they carry a button label).  The registry reads the two spaces
#: separately on purpose — a money marker must not fire on unrelated field text
#: — so the split has to happen here, not in the classifier.
_TARGET_TYPES = frozenset({"button", "submit", "link", "menuitem", "tab"})


def _text(value: Any) -> str:
    return str(value or "").strip()


def _node_get(node: Any, attr: str) -> Any:
    if isinstance(node, Mapping):
        return node.get(attr)
    return getattr(node, attr, None)


def subject_from_journey_graph(
    journey: Any,
    nodes: Sequence[Any],
    *,
    edge_labels: Sequence[str] = (),
    invariant_links: Sequence[str] = (),
    repo_markers: Sequence[str] = (),
) -> dict[str, str]:
    """Project a journey's graph rows into a criticality subject.

    The projection is VALUE-FREE by construction: paths, accessible control
    names and structural verbs cross; a committed answer never does.  That is
    not a nicety — the subject is stamped onto an API response and into the
    ranking evidence, and a criticality registry is the last place a user's date
    of birth should turn up.

    ``edge_labels`` are the advance triggers the walk actually clicked.  They are
    projected as BUTTON labels because that is what they are, and because the
    control that commits a funnel is frequently absent from any single node's
    inventory (it is read on the page it leaves, not the page it reaches).
    """
    pages: list[dict[str, Any]] = []
    for node in nodes or ():
        url = _text(_node_get(node, "url"))
        path = urlsplit(url).path if url else ""
        fields: list[str] = []
        targets: list[str] = []
        for control in (_node_get(node, "controls_inventory") or ()):
            if not isinstance(control, Mapping):
                continue
            name = _text(control.get("name"))
            if not name:
                continue
            kind = _text(control.get("type")).lower()
            (targets if kind in _TARGET_TYPES else fields).append(name)
        verbs: list[str] = []
        if _node_get(node, "is_boundary"):
            # The registry expresses "multi-page submission" through this verb;
            # a boundary node IS the submit, whatever the control was called.
            verbs.append("submit")
        pages.append({
            "host": urlsplit(url).netloc if url else "",
            "path": path,
            "verbs": verbs,
            "fields": fields,
            "targets": targets,
        })

    subject = criticality.subject_from_journey(
        {"kind": "journey", "pages": pages},
        invariant_links=invariant_links,
        repo_markers=repo_markers,
    )
    # The advance triggers join the BUTTON space.  Appended rather than merged
    # into a page so the multi-page-submit inference above keeps reading the
    # structural evidence (pages that carry a boundary), not a label's wording.
    extra = " ".join(_text(label) for label in edge_labels if _text(label))
    if extra:
        subject["button_label"] = (subject.get("button_label", "") + " " + extra).strip()
    return subject


def evaluate_journey(
    journey: Any,
    nodes: Sequence[Any],
    *,
    edge_labels: Sequence[str] = (),
    pack: Any = None,
    registry_version: str | None = None,
    invariant_links: Sequence[str] = (),
    repo_markers: Sequence[str] = (),
) -> dict[str, Any]:
    """Band ONE journey, carrying the registry's evidence through verbatim.

    Returns the ``criticality.evaluate`` result plus the ``subject`` it was
    banded from, so a reviewer can see exactly what the classifier read.  A
    journey no signal matches FAILS UP to P1 — that is the registry's own
    doctrine and this adapter does not soften it.
    """
    subject = subject_from_journey_graph(
        journey, nodes, edge_labels=edge_labels,
        invariant_links=invariant_links, repo_markers=repo_markers,
    )
    result = criticality.evaluate(
        subject, pack=pack, registry_version=registry_version)
    return {**result, "subject": subject}


def band_order(band: str) -> int:
    """Position of a band in ``criticality.BANDS`` (P0 first).

    Derived from the registry's own public tuple rather than a second copy of
    the ordering: a band the registry adds must not silently sort last here, and
    an unrecognised one takes the FAIL-UP position, never the least-critical.
    """
    bands = list(criticality.BANDS)
    normalized = str(band or "").strip().upper()
    if normalized in bands:
        return bands.index(normalized)
    return bands.index(criticality.FAIL_UP_BAND)


def rank_key(entry: Mapping[str, Any]) -> tuple:
    """The total, deterministic sort key (see the module docstring).

    Exposed because the API has to be able to state HOW it ordered, and a
    ranking whose key is buried inside a ``sorted`` call is a ranking nobody can
    reproduce or test.
    """
    band = str((entry.get("criticality") or {}).get("band") or criticality.FAIL_UP_BAND)
    return (
        band_order(band),
        -int(entry.get("deepest_steps") or 0),
        -int(entry.get("paths_completed") or 0),
        -int(entry.get("boundary_nodes") or 0),
        -int(entry.get("endpoints_observed") or 0),
        str(entry.get("journey_id") or ""),
    )


def rank(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Order journeys most-critical-first and stamp a 1-based ``rank``.

    Total and stable over the stored evidence: two calls over the same rows
    return the same order, and the ``rank`` field is written here rather than by
    a caller's enumerate so every surface that shows a rank shows THIS one.
    """
    ordered = sorted((dict(e) for e in entries or ()), key=rank_key)
    for position, entry in enumerate(ordered, start=1):
        entry["rank"] = position
    return ordered


def top_n(
    entries: Sequence[Mapping[str, Any]], n: int = TOP_N_DEFAULT,
) -> list[dict[str, Any]]:
    """The ranked head of the list.  Ranks are assigned over the WHOLE set
    first, so the twentieth entry is rank 20 of everything, never rank 20 of a
    slice somebody already truncated."""
    return rank(entries)[: max(0, int(n))]


def boundary_node_count(nodes: Sequence[Any]) -> int:
    """How many nodes on this journey are commit boundaries."""
    return sum(1 for node in nodes or () if _node_get(node, "is_boundary"))


def endpoint_count(nodes: Sequence[Any]) -> int:
    """How many DISTINCT (method, path) endpoints this journey's nodes observed
    — the size of the backend surface a generated spec could hold it to."""
    seen: set[tuple[str, str]] = set()
    for node in nodes or ():
        for endpoint in endpoints_of(node):
            seen.add((endpoint["method"], endpoint["path"]))
    return len(seen)


__all__ = [
    "TOP_N_DEFAULT",
    "band_order",
    "subject_from_journey_graph",
    "evaluate_journey",
    "rank_key",
    "rank",
    "top_n",
    "boundary_node_count",
    "endpoint_count",
]
