"""QE-Central S4 — deterministic journey synthesis (value-free, locator-free).

Turns crawl substrate (``page_visits`` / ``page_actions``, written by
:mod:`app.substrate.writer`) into governed regression SCENARIOS: ordered,
value-free, locator-free journey skeletons with a STABLE identity and a
change-detecting fingerprint.  ZERO LLM, deterministic, $0.

Two hashes, two jobs (design §3.4):

  * ``scenario_id = uuid5(app_id + route-identity skeleton)`` — the STABLE
    identity.  It is computed over the journey's page-route sequence only, so
    editing a field or a value keeps the id fixed and the scenario diffs as
    ``changed`` rather than reappearing as a brand-new ``new`` row.
  * ``fingerprint = sha256(full value-free journey detail)`` — the CHANGE
    signal.  It moves iff pages / verbs / field-labels / target-labels change;
    it NEVER moves on a value change (values are never captured into the
    skeleton) or a locator change (locators are never captured at all).

Diff semantics against the stored scenario set:

  ================  ==========================================================
  ``new``           scenario_id unseen                → enters the approval queue
  ``changed``       scenario_id seen, fingerprint ≠   → re-enters the queue
  ``unchanged``     scenario_id seen, fingerprint ==  → auto-carry approval, ZERO touch
  ``missing``       stored scenario_id absent fresh   → shrinkage path (never deleted)
  ================  ==========================================================

Journey shapes emitted (the ``_split_revisit_branch`` idea — generator.py:
712-729 — reimplemented over substrate rows, plus per-terminal-form flows):

  * ``trunk``         — the main path with the first clean revisit branch removed
  * ``revisit_branch``— the demonstrated side-exploration A..A (if any)
  * ``terminal_form`` — each intermediate submit becomes its own certifiable flow

An UNCHANGED scenario carries its prior approval forward untouched: synthesis
only ever writes journey / band / fingerprint / diff_state / registry fields —
it NEVER disturbs ``review`` / ``approved_snapshot`` / ``tier`` (those belong to
:mod:`app.services.approval` and :mod:`app.services.tier_label`), so no human
touch is spent re-approving something that did not move.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from sqlalchemy import select

from nexus_sdk.db.models import PageActionRow, PageVisitRow

from ..db import (
    tenant_scoped_qec_session,
    tenant_scoped_substrate_session,
    utc_now,
)
from ..db.gov_models import CertifiedInvariantRow, ScenarioRow
from . import criticality

logger = logging.getLogger(__name__)

# ── Diff states ───────────────────────────────────────────────────────────
DIFF_NEW = "new"
DIFF_CHANGED = "changed"
DIFF_UNCHANGED = "unchanged"
DIFF_MISSING = "missing"

# ── Journey kinds ─────────────────────────────────────────────────────────
KIND_TRUNK = "trunk"
KIND_REVISIT_BRANCH = "revisit_branch"
KIND_TERMINAL_FORM = "terminal_form"

#: page_visits rows the loader excludes (honest gap-markers, never generatable).
_MISSING_PAGE_SOURCE = "missing_page"

_WS_RE = re.compile(r"\s+")


# ── Normalisation (value-free, locator-free, order-invariant) ─────────────

def _norm_label(value: Any) -> str:
    """Collapse whitespace + casefold an accessible label (never a locator)."""
    return _WS_RE.sub(" ", str(value or "").strip()).casefold()


def _norm_host(value: Any) -> str:
    return str(value or "").strip().casefold()


def _norm_path(value: Any) -> str:
    """Path only, casefolded, trailing slash stripped, query/fragment dropped
    (a query VALUE must never move the fingerprint)."""
    p = str(value or "").strip().casefold()
    p = p.split("?", 1)[0].split("#", 1)[0]
    if len(p) > 1:
        p = p.rstrip("/")
    return p


# ── Page node (the substrate projection build_journeys consumes) ──────────

@dataclass(frozen=True)
class PageNode:
    """One contiguous page visit projected to value-free/locator-free fields.

    ``fields`` are accessible form-field labels, ``targets`` are the accessible
    names of the controls acted on, ``verbs`` are the recorded action verbs —
    NONE of these carry a value or a selector.
    """

    sequence_index: int
    host: str
    path: str
    verbs: tuple[str, ...] = ()
    fields: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    has_submit: bool = False


def canonical_page(node: PageNode) -> dict:
    """Canonicalise a node into a fingerprint-stable page step (sorted, deduped).

    Sorting makes the fingerprint invariant to capture ORDER; the fields are
    already value-free/locator-free, so it is also invariant to value/locator
    churn.
    """
    return {
        "host": _norm_host(node.host),
        "path": _norm_path(node.path),
        "verbs": sorted({str(v).strip().lower() for v in node.verbs if str(v).strip()}),
        "fields": sorted({_norm_label(f) for f in node.fields if str(f).strip()}),
        "targets": sorted({_norm_label(t) for t in node.targets if str(t).strip()}),
    }


# ── The trunk/branch split (generator.py:712-729 idea over rows) ──────────

def split_revisit_branch(nodes: Sequence[PageNode]) -> tuple[list[PageNode], list[PageNode]]:
    """(trunk, branch): first clean revisit pair A..A (same canonical URL, ≥1
    page between, trunk stays ≥2) — the middle is a demonstrated
    side-exploration.  No revisit → (nodes, [])."""
    canon = [f"{_norm_host(n.host)}{_norm_path(n.path)}" for n in nodes]
    for i in range(len(nodes) - 2):
        if not canon[i]:
            continue
        for j in range(i + 2, len(nodes)):
            if canon[j] == canon[i]:
                trunk = list(nodes[:i + 1]) + list(nodes[j + 1:])
                branch = list(nodes[i:j + 1])
                if len(trunk) >= 2 and len(branch) >= 2:
                    return trunk, branch
    return list(nodes), []


# ── Journey ───────────────────────────────────────────────────────────────

def _canonical_json(payload: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, compact separators)."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    )


@dataclass
class Journey:
    """One deterministic journey: an ordered list of value-free page steps."""

    kind: str
    pages: list[dict] = field(default_factory=list)

    def fingerprint(self) -> str:
        """sha256 over the FULL value-free journey detail (change signal)."""
        return hashlib.sha256(
            _canonical_json({"kind": self.kind, "pages": self.pages}).encode("utf-8")
        ).hexdigest()

    def identity_key(self, app_id: str) -> str:
        """The STABLE route-identity string (value-free, field-free)."""
        route = " > ".join(f"{p.get('host', '')}{p.get('path', '')}" for p in self.pages)
        return f"{app_id}|{self.kind}|{route}"

    def scenario_id(self, app_id: str) -> str:
        """uuid5 of the identity key — stable across re-crawls."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, self.identity_key(app_id)))

    def name(self) -> str:
        """A human journey name from the page path tails (value-free)."""
        segs: list[str] = []
        for p in self.pages:
            path = str(p.get("path") or "").strip("/")
            seg = path.split("/")[-1] if path else str(p.get("host") or "")
            segs.append(seg or "page")
        return (" -> ".join(segs) or "journey")[:500]

    def to_journey_json(self) -> dict:
        """The JSONB value stored on ``qec_scenarios.journey``."""
        return {"kind": self.kind, "pages": self.pages}


def _journey_from_nodes(nodes: Sequence[PageNode], kind: str) -> Journey:
    return Journey(kind=kind, pages=[canonical_page(n) for n in nodes])


def _terminal_form_journeys(trunk_nodes: Sequence[PageNode]) -> list[Journey]:
    """One journey per INTERMEDIATE submit (the last node is already the trunk).

    Each becomes its own certifiable flow; identical prefixes are de-duplicated
    downstream by ``scenario_id`` (:func:`compute_diff`).
    """
    out: list[Journey] = []
    for k in range(len(trunk_nodes) - 1):  # exclude the last node (== trunk)
        if trunk_nodes[k].has_submit:
            prefix = trunk_nodes[:k + 1]
            if len(prefix) >= 2:
                out.append(_journey_from_nodes(prefix, KIND_TERMINAL_FORM))
    return out


def build_journeys(
    nodes: Sequence[PageNode], *, include_terminal_forms: bool = True,
) -> list[Journey]:
    """Build the trunk journey (+ revisit branch, + per-terminal-form flows)."""
    nodes = [n for n in nodes if n is not None]
    if not nodes:
        return []
    trunk_nodes, branch_nodes = split_revisit_branch(nodes)
    journeys = [_journey_from_nodes(trunk_nodes, KIND_TRUNK)]
    if branch_nodes:
        journeys.append(_journey_from_nodes(branch_nodes, KIND_REVISIT_BRANCH))
    if include_terminal_forms:
        journeys.extend(_terminal_form_journeys(trunk_nodes))
    return journeys


# ── Diff result value objects ─────────────────────────────────────────────

@dataclass
class ScenarioDiff:
    """One synthesised scenario with its diff verdict."""

    scenario_id: str
    name: str
    kind: str
    criticality_band: str
    criticality_evidence: list
    registry_version: str
    fingerprint: str
    diff_state: str
    journey: dict
    prior_fingerprint: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class SynthesisResult:
    """The full diff of a crawl against the stored scenario set."""

    app_id: str
    scenarios: list[ScenarioDiff]
    counts: dict
    queue: list[str]
    tenant_id: str = ""
    artifact_id: str = ""
    extractor_version: str | None = None

    def as_dict(self) -> dict:
        return {
            "app_id": self.app_id,
            "tenant_id": self.tenant_id,
            "artifact_id": self.artifact_id,
            "extractor_version": self.extractor_version,
            "counts": self.counts,
            "queue": list(self.queue),
            "scenarios": [s.as_dict() for s in self.scenarios],
        }


def compute_diff(
    app_id: str,
    journeys: Sequence[Journey],
    stored_fingerprints: Mapping[str, str],
    *,
    pack: Any = None,
    registry_version: str | None = None,
    invariant_links_by_scenario: Mapping[str, Sequence[str]] | None = None,
) -> SynthesisResult:
    """Diff fresh journeys against ``{scenario_id: fingerprint}`` (PURE).

    Bands each fresh scenario via :func:`criticality.evaluate` (fail-up-safe),
    de-duplicates identical journeys by ``scenario_id`` (a terminal-form prefix
    equal to the trunk collapses to one), and flags every stored scenario_id
    absent from the fresh set as ``missing`` (shrinkage — never deleted here).
    """
    invariant_links_by_scenario = dict(invariant_links_by_scenario or {})
    stored_fingerprints = dict(stored_fingerprints or {})

    counts = {DIFF_NEW: 0, DIFF_CHANGED: 0, DIFF_UNCHANGED: 0, DIFF_MISSING: 0}
    scenarios: list[ScenarioDiff] = []
    seen: set[str] = set()

    for journey in journeys:
        sid = journey.scenario_id(app_id)
        if sid in seen:
            continue
        seen.add(sid)
        fingerprint = journey.fingerprint()
        prior = stored_fingerprints.get(sid)
        if prior is None:
            state = DIFF_NEW
        elif prior == fingerprint:
            state = DIFF_UNCHANGED
        else:
            state = DIFF_CHANGED
        counts[state] += 1

        subject = criticality.subject_from_journey(
            journey.to_journey_json(),
            invariant_links=invariant_links_by_scenario.get(sid, ()),
        )
        crit = criticality.evaluate(subject, pack=pack, registry_version=registry_version)

        scenarios.append(ScenarioDiff(
            scenario_id=sid,
            name=journey.name(),
            kind=journey.kind,
            criticality_band=crit["band"],
            criticality_evidence=crit["evidence"],
            registry_version=crit["registry_version"],
            fingerprint=fingerprint,
            diff_state=state,
            journey=journey.to_journey_json(),
            prior_fingerprint=prior,
        ))

    for sid, prior in stored_fingerprints.items():
        if sid in seen:
            continue
        counts[DIFF_MISSING] += 1
        scenarios.append(ScenarioDiff(
            scenario_id=sid,
            name="",
            kind="",
            criticality_band="",
            criticality_evidence=[],
            registry_version=registry_version or "",
            fingerprint=prior,
            diff_state=DIFF_MISSING,
            journey={},
            prior_fingerprint=prior,
        ))

    counts["total"] = len(scenarios)
    queue = [s.scenario_id for s in scenarios if s.diff_state in (DIFF_NEW, DIFF_CHANGED)]
    return SynthesisResult(app_id=app_id, scenarios=scenarios, counts=counts, queue=queue)


# ── Substrate read path (mirrors service.py:44-58 latest-wins selection) ──

async def _latest_version(session, model, artifact_id: str) -> str | None:
    """extractor_version of the most-recently-written row (created_at MAX).

    Byte-for-byte the factory's latest-wins selection (test_factory/
    service.py:44-58) so synthesis reads exactly the version the generator
    compiles from — never a stale or mixed version.
    """
    return (
        await session.execute(
            select(model.extractor_version)
            .where(model.artifact_id == artifact_id)
            .order_by(model.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def known_labels_for_artifact(tenant_id: str, artifact_id: str) -> list[str]:
    """Distinct form-field LABELS across an artifact's current page_visits — the
    grounding vocabulary the brief compiler (ANSWERS P2) validates a proposal
    against.  Labels ONLY (snapshot KEYS), never their values, never a locator —
    the same value-free discipline as :func:`read_page_nodes`."""
    if not artifact_id:
        return []
    async with tenant_scoped_substrate_session(tenant_id) as session:
        version = await _latest_version(session, PageVisitRow, artifact_id)
        if version is None:
            return []
        rows = (
            await session.execute(
                select(PageVisitRow).where(
                    PageVisitRow.artifact_id == artifact_id,
                    PageVisitRow.extractor_version == version,
                )
            )
        ).scalars().all()
    labels: set[str] = set()
    for v in rows:
        if (getattr(v, "source", "") or "") == _MISSING_PAGE_SOURCE:
            continue
        labels |= {str(k) for k in (v.form_snapshot_signals or {}).keys()}
        labels |= {str(k) for k in (v.form_snapshot or {}).keys()}
    return sorted(l for l in labels if l.strip())


async def field_inventory_for_artifact(tenant_id: str, artifact_id: str) -> list[dict]:
    """Per-field inventory ``[{label, type, options, required}]`` across an artifact's
    current page_visits — the grounding vocabulary the Seed Manifest (Phase 1) /
    Data Agent (Phase 3) classify over. Projects ``form_snapshot_signals`` VALUES
    (``{type, options, required}`` per label) — never a filled value, never a locator,
    the same value-free discipline as :func:`known_labels_for_artifact`. Deduped by
    label; the richest signal (most observed options) wins when a label recurs."""
    if not artifact_id:
        return []
    async with tenant_scoped_substrate_session(tenant_id) as session:
        version = await _latest_version(session, PageVisitRow, artifact_id)
        if version is None:
            return []
        rows = (
            await session.execute(
                select(PageVisitRow).where(
                    PageVisitRow.artifact_id == artifact_id,
                    PageVisitRow.extractor_version == version,
                )
            )
        ).scalars().all()
    best: dict[str, dict] = {}
    for v in rows:
        if (getattr(v, "source", "") or "") == _MISSING_PAGE_SOURCE:
            continue
        for label, sig in (v.form_snapshot_signals or {}).items():
            key = str(label).strip()
            if not key:
                continue
            sig = sig if isinstance(sig, dict) else {}
            options = [str(o) for o in (sig.get("options") or [])]
            entry = {
                "label": key,
                "type": str(sig.get("type") or "text"),
                "options": options,
                "required": bool(sig.get("required")),
            }
            # A dependent/conditional field (options/existence gated on a driver field) —
            # the crawler's ACT-THEN-DIFF pass tags it so the coverage ledger reads it as
            # conditional, not as an always-present fixed control.
            if sig.get("depends_on"):
                entry["depends_on"] = str(sig.get("depends_on"))
            prev = best.get(key)
            # Keep the richest signal for a recurring label (more observed options).
            if prev is None or len(options) > len(prev.get("options") or []):
                best[key] = entry
    return [best[k] for k in sorted(best)]


async def known_routes_for_artifact(tenant_id: str, artifact_id: str) -> list[str]:
    """Distinct URL routes (``url_path`` + any hash route in ``url_query``/location)
    the crawl already reached — the GROUNDING vocabulary the exploration planner
    validates an LLM-proposed priority pattern against (a pattern that matches no
    real route is rejected, so the planner can never chase a hallucinated section).
    Path shape only, never values."""
    if not artifact_id:
        return []
    async with tenant_scoped_substrate_session(tenant_id) as session:
        version = await _latest_version(session, PageVisitRow, artifact_id)
        if version is None:
            return []
        rows = (
            await session.execute(
                select(PageVisitRow).where(
                    PageVisitRow.artifact_id == artifact_id,
                    PageVisitRow.extractor_version == version,
                )
            )
        ).scalars().all()
    routes: set[str] = set()
    for v in rows:
        if (getattr(v, "source", "") or "") == _MISSING_PAGE_SOURCE:
            continue
        path = str(getattr(v, "url_path", "") or "").strip()
        if path:
            routes.add(path)
        # hash-route SPAs record the route in the location, not url_path.
        loc = str(getattr(v, "location", "") or "")
        if "#" in loc:
            frag = loc.split("#", 1)[1]
            if frag:
                routes.add("#" + frag)
    return sorted(r for r in routes if r.strip())


async def known_value_nodes_for_artifact(tenant_id: str, artifact_id: str) -> list[dict]:
    """Displayed value nodes ``[{label, source_hint}]`` captured across an artifact's
    current page_visits (ANSWERS P1.B) — the grounding TARGETS the brief compiler ties
    an expected outcome to when the client gives no source_hint of their own.  Selector
    + label only (the captured text is a runtime observation, not authored expectation)."""
    if not artifact_id:
        return []
    async with tenant_scoped_substrate_session(tenant_id) as session:
        version = await _latest_version(session, PageVisitRow, artifact_id)
        if version is None:
            return []
        rows = (
            await session.execute(
                select(PageVisitRow).where(
                    PageVisitRow.artifact_id == artifact_id,
                    PageVisitRow.extractor_version == version,
                )
            )
        ).scalars().all()
    nodes: list[dict] = []
    seen: set[str] = set()
    for v in rows:
        for dv in (getattr(v, "displayed_values", None) or []):
            if not isinstance(dv, dict):
                continue
            selector = str(dv.get("selector") or "").strip()
            if not selector:
                continue
            label = str(dv.get("label") or "").strip()
            key = f"{selector}|{label}"
            if key in seen:
                continue
            seen.add(key)
            nodes.append({"label": label, "source_hint": selector})
    return nodes


async def value_candidates_for_artifact(tenant_id: str, artifact_id: str) -> list[dict]:
    """#2 value-oracle PROVING wiring — the crawl-side CANDIDATE expected values.

    Reads the current-version ``displayed_values`` nodes the crawler classified as
    candidate outcomes (``value_candidate == "true"``: a rendered premium / total /
    decision) and returns them for CONFIRMATION:
    ``[{label, source_hint, text, value_type, confidence, reason}]``.

    Doctrine: the captured ``text`` is a RUNTIME OBSERVATION surfaced only as a
    review pre-fill — the confirm endpoint requires a human-AUTHORED expected
    value; nothing here becomes an assertion without that confirmation.  Additive:
    the existing ``known_value_nodes_for_artifact`` projection (and the brief
    prompt it feeds) is untouched."""
    if not artifact_id:
        return []
    async with tenant_scoped_substrate_session(tenant_id) as session:
        version = await _latest_version(session, PageVisitRow, artifact_id)
        if version is None:
            return []
        rows = (
            await session.execute(
                select(PageVisitRow).where(
                    PageVisitRow.artifact_id == artifact_id,
                    PageVisitRow.extractor_version == version,
                )
            )
        ).scalars().all()
    out: list[dict] = []
    seen: set[str] = set()
    for v in rows:
        for dv in (getattr(v, "displayed_values", None) or []):
            if not isinstance(dv, dict):
                continue
            if str(dv.get("value_candidate") or "").strip().lower() != "true":
                continue
            selector = str(dv.get("selector") or "").strip()
            if not selector or selector in seen:
                continue
            seen.add(selector)
            try:
                confidence = float(dv.get("value_confidence") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            out.append({
                "label": str(dv.get("label") or "").strip(),
                "source_hint": selector,
                "text": str(dv.get("text") or "").strip(),
                "value_type": str(dv.get("value_type") or "").strip(),
                "confidence": confidence,
                "reason": str(dv.get("value_reason") or "").strip(),
            })
    out.sort(key=lambda c: -c["confidence"])
    return out


async def read_page_nodes(tenant_id: str, artifact_id: str) -> tuple[list[PageNode], str | None]:
    """Read the CURRENT-version page_visits + page_actions as :class:`PageNode`s.

    Value-free by construction: only labels/verbs are lifted — never
    ``page_actions.value`` or ``form_snapshot`` VALUES, never a locator.
    """
    async with tenant_scoped_substrate_session(tenant_id) as session:
        visit_version = await _latest_version(session, PageVisitRow, artifact_id)
        if visit_version is None:
            return [], None

        visit_rows = (
            await session.execute(
                select(PageVisitRow)
                .where(
                    PageVisitRow.artifact_id == artifact_id,
                    PageVisitRow.extractor_version == visit_version,
                )
                .order_by(PageVisitRow.sequence_index.asc())
            )
        ).scalars().all()

        visit_ids = [v.page_visit_id for v in visit_rows]
        actions_by_visit: dict[str, list[PageActionRow]] = {}
        if visit_ids:
            action_version = await _latest_version(session, PageActionRow, artifact_id)
            if action_version:
                action_rows = (
                    await session.execute(
                        select(PageActionRow)
                        .where(
                            PageActionRow.page_visit_id.in_(visit_ids),
                            PageActionRow.extractor_version == action_version,
                        )
                        .order_by(PageActionRow.subaction_index.asc())
                    )
                ).scalars().all()
                for a in action_rows:
                    actions_by_visit.setdefault(a.page_visit_id, []).append(a)

    nodes: list[PageNode] = []
    for v in visit_rows:
        # MISSING_PAGE rows are honest gap-markers — never a synthesizable page.
        if (getattr(v, "source", "") or "") == _MISSING_PAGE_SOURCE:
            continue
        acts = actions_by_visit.get(v.page_visit_id, [])
        # Field LABELS only (keys), never their values.
        field_labels = set((v.form_snapshot_signals or {}).keys()) | set(
            (v.form_snapshot or {}).keys()
        )
        verbs = [a.verb for a in acts if a.verb]
        targets = [a.target_label for a in acts if a.target_label]
        has_submit = any((a.verb or "").strip().lower() == "submit" for a in acts)
        nodes.append(PageNode(
            sequence_index=v.sequence_index,
            host=v.canonical_host or v.url_host or "",
            path=v.url_path or "",
            verbs=tuple(verbs),
            fields=tuple(sorted(field_labels)),
            targets=tuple(targets),
            has_submit=has_submit,
        ))
    return nodes, visit_version


# ── qecentral reads/writes ────────────────────────────────────────────────

async def _load_stored_scenarios(session, tenant_id: str, app_id: str) -> dict[str, ScenarioRow]:
    rows = (
        await session.execute(
            select(ScenarioRow).where(
                ScenarioRow.tenant_id == tenant_id,
                ScenarioRow.app_id == app_id,
                ScenarioRow.status == "active",
            )
        )
    ).scalars().all()
    return {r.scenario_id: r for r in rows}


async def _invariant_links(session, tenant_id: str, app_id: str) -> dict[str, list[str]]:
    """Map ``scenario_id → [invariant_id, ...]`` for criticality's presence
    signal.  A scenario linked to any certified invariant bands P0."""
    rows = (
        await session.execute(
            select(CertifiedInvariantRow).where(
                CertifiedInvariantRow.tenant_id == tenant_id,
                CertifiedInvariantRow.app_id == app_id,
            )
        )
    ).scalars().all()
    out: dict[str, list[str]] = {}
    for r in rows:
        for sid in (r.linked_scenario_ids or []):
            out.setdefault(str(sid), []).append(r.invariant_id)
    return out


async def _persist(
    session, *, tenant_id: str, app_id: str, artifact_id: str,
    result: SynthesisResult, stored_rows: Mapping[str, ScenarioRow],
) -> None:
    """Upsert scenario rows from the diff.

    Synthesis writes ONLY journey / band / fingerprint / diff_state / registry
    / source_artifact.  It never touches ``review`` / ``approved_snapshot`` /
    ``tier`` — so an UNCHANGED row keeps its prior approval (zero touch) and a
    CHANGED row keeps its last-approved snapshot for audit while ``diff_state``
    re-queues it.  A MISSING scenario is FLAGGED (``diff_state='missing'``),
    never deleted — the shrinkage guard adjudicates it.
    """
    now = utc_now()
    for scenario in result.scenarios:
        if scenario.diff_state == DIFF_MISSING:
            row = stored_rows.get(scenario.scenario_id)
            if row is not None and row.diff_state != DIFF_MISSING:
                row.diff_state = DIFF_MISSING
                row.updated_at = now
            continue

        row = stored_rows.get(scenario.scenario_id)
        if row is None:
            row = ScenarioRow(
                scenario_id=scenario.scenario_id,
                tenant_id=tenant_id,
                app_id=app_id,
            )
            session.add(row)
        row.source_artifact_id = artifact_id
        row.name = scenario.name
        row.journey = scenario.journey
        row.criticality_band = scenario.criticality_band
        row.criticality_evidence = scenario.criticality_evidence
        row.registry_version = scenario.registry_version
        row.fingerprint = scenario.fingerprint
        row.diff_state = scenario.diff_state
        row.status = "active"
        row.updated_at = now

    await session.flush()


async def synthesize(
    tenant_id: str,
    app_id: str,
    artifact_id: str,
    *,
    persist: bool = True,
) -> SynthesisResult:
    """Synthesise + diff + (optionally) persist scenarios for one crawl.

    Reads the current-version substrate for ``artifact_id`` (nexus DB, role
    ``qec_substrate``), builds deterministic journeys, bands each via the
    tenant's ACTIVE criticality registry (seed-pack fallback), diffs against the
    stored scenario set (qecentral DB, role ``qec``), and — when ``persist`` —
    upserts the scenario rows.  UNCHANGED scenarios carry their prior approval
    forward untouched (zero human touch); only NEW/CHANGED land in ``queue``.

    ``tenant_id`` is required (RLS scopes every read/write); the design's
    ``synthesize(app_id, artifact_id)`` shorthand omits it, but no honest
    tenant-scoped operation can.
    """
    nodes, extractor_version = await read_page_nodes(tenant_id, artifact_id)
    journeys = build_journeys(nodes)
    signals, registry_version = await criticality.load_active_pack(tenant_id)

    async with tenant_scoped_qec_session(tenant_id) as session:
        stored_rows = await _load_stored_scenarios(session, tenant_id, app_id)
        stored_fingerprints = {sid: r.fingerprint for sid, r in stored_rows.items()}
        invariant_links = await _invariant_links(session, tenant_id, app_id)

        result = compute_diff(
            app_id,
            journeys,
            stored_fingerprints,
            pack=signals,
            registry_version=registry_version,
            invariant_links_by_scenario=invariant_links,
        )
        result.tenant_id = tenant_id
        result.artifact_id = artifact_id
        result.extractor_version = extractor_version

        if persist:
            await _persist(
                session,
                tenant_id=tenant_id,
                app_id=app_id,
                artifact_id=artifact_id,
                result=result,
                stored_rows=stored_rows,
            )

    logger.info(
        "qec.synthesis.completed",
        extra={
            "tenant_id": tenant_id,
            "app_id": app_id,
            "artifact_id": artifact_id,
            "extractor_version": extractor_version,
            "counts": result.counts,
            "queue_size": len(result.queue),
            "persisted": persist,
        },
    )
    return result


__all__ = [
    "DIFF_NEW", "DIFF_CHANGED", "DIFF_UNCHANGED", "DIFF_MISSING",
    "KIND_TRUNK", "KIND_REVISIT_BRANCH", "KIND_TERMINAL_FORM",
    "PageNode",
    "Journey",
    "ScenarioDiff",
    "SynthesisResult",
    "canonical_page",
    "split_revisit_branch",
    "build_journeys",
    "compute_diff",
    "read_page_nodes",
    "synthesize",
]
