"""QE-Central S5 — the fingerprint store (design §3.5, ``cycle/fingerprints.py``).

WHAT IT IS
==========
An app's regression baseline is a set of per-page *structural fingerprints*.
This module seeds and diffs those fingerprints so the change detector can decide
which pages (and which navigating controls) changed between two crawls.

Baselines are seeded from ``GET /test-factory/{artifact_id}/journey-graph``
(test_factory.py:1333-1363): that endpoint builds the app's navigation graph
from grounded steps — page nodes keyed by :func:`normalize_page_key` (mirrors
``journey_graph.page_key``) and control-attributed transition edges carrying a
``control_fp`` (mirrors ``control_ledger.control_fingerprint``, page:1354 /
fp:1360).  A page fingerprint here is therefore over the app's **navigation
topology** — its transition set and the identity of every navigating control —
NOT a full DOM snapshot.  That is exactly what the endpoint can prove, and it is
fail-safe: a *coarser* fingerprint can only over-report change, never hide it.

THE FAIL-SAFE CAVEAT (design open decision #6, VERIFIED)
=======================================================
``platform/api/app/services/diff_and_heal/journey_graph.py`` is ABSENT from the
local repo (VM-only per the repo↔VM divergence), so the journey-graph endpoint
may be unavailable in some environments.  Every uncertain outcome fails SAFE —
toward CHANGED, never toward a false "no change":

  * endpoint unavailable / non-200 / non-JSON / unparseable body  → the snapshot
    is ``unavailable``  → the detector treats EVERY baseline page as changed;
  * a live graph that parses to ZERO computable pages             → ``unavailable``
    (we learned nothing — never "everything was deleted");
  * an individual page whose fingerprint cannot be computed        → a
    ``computable=False`` page → treated CHANGED;
  * a baseline page ABSENT from an otherwise-computable live graph  → a *vanished*
    page → a ``possible_deletion`` honest gap (positive evidence of removal).

Only the FETCH is deferred to the VM.  Every type and diff helper here is pure
logic over already-fetched fingerprint dicts, so the whole store is
unit-testable locally against synthetic inputs.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping

logger = logging.getLogger(__name__)

# sha256 prefix length for control fingerprints — byte-identical to
# control_ledger._FP_LEN so a control_fp derived here equals the one the
# journey-graph endpoint emits for the same (page, label, kind).
_FP_LEN = 40

#: The kind of async callable the store uses to fetch a journey graph.  Injected
#: so tests can supply a synthetic graph (or a failure) without any network.
JourneyGraphFetcher = Callable[..., Awaitable["dict | None"]]

# Recognised container keys in a journey-graph response.  The endpoint's output
# shape is VM-only (journey_graph.build_journey_graph), so we accept the common
# node/edge spellings and fail SAFE (→ unavailable) on anything unrecognisable.
_NODE_KEYS = ("nodes", "pages")
_EDGE_KEYS = ("edges", "transitions")
_PAGE_KEY_FIELDS = ("page_key", "key", "url_path", "path", "id")
_EDGE_FROM_FIELDS = ("from_page", "from", "source", "src")
_EDGE_TO_FIELDS = ("to_page", "to", "target", "dst", "next_page")


# ─── Normalisation (vendored — platform.api is not importable across containers) ──

def _norm(s: str | None) -> str:
    """Trim + collapse whitespace + lowercase (mirrors control_ledger._norm)."""
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def normalize_page_key(url: str | None) -> str:
    """The PATH portion of a URL (no scheme/host/query/hash).

    Byte-for-byte the same algorithm as ``journey_graph.page_key`` /
    ``control_ledger.page_key`` (control_ledger.py:81-91) so a page_key computed
    here matches the one the endpoint emits and the one a compiled case's step
    carries.  Accepts either a full URL or a bare ``/path`` (a leading ``/`` reads
    as a host-less path exactly as the source function treats it).  Empty → "".
    """
    u = (url or "").strip()
    if not u:
        return ""
    after = u.split("://", 1)[-1]
    path = after.split("/", 1)[1] if "/" in after else ""
    path = path.split("?", 1)[0].split("#", 1)[0].strip("/")
    return "/" + path if path else "/"


def derive_control_fp(
    label: str | None, kind: str | None = "", page_key: str | None = "",
) -> str:
    """Stable app-level identity for a control (mirrors control_fingerprint,
    control_ledger.py:110-126): ``sha256(page\\nlabel\\nkind)[:40]``.

    Returns "" when there is no groundable ``label`` — an unlabelled control has
    no honest key, so it simply does not join the ledger (never a false key).
    """
    lab = _norm(label)
    if not lab:
        return ""
    raw = "\n".join((normalize_page_key(page_key), lab, _norm(kind)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:_FP_LEN]


# ─── Core types ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PageFingerprint:
    """A single page's structural fingerprint.

    ``structural_hash``  — sha256 over the page's canonicalised outgoing-edge set
                           (each edge: ``to_page | verb | control_fp``); stable
                           across cosmetic re-renders, flips on any topology or
                           navigating-control change.
    ``control_fps``      — sorted, de-duplicated fingerprints of the navigating
                           controls anchored on this page (the join key a case's
                           step matches against).
    ``computable``       — False marks an uncomputable page (FAIL-SAFE → CHANGED).
    ``last_verified_at`` — ISO8601 of the crawl that produced it (provenance only).
    """

    structural_hash: str
    control_fps: tuple[str, ...]
    computable: bool = True
    last_verified_at: str | None = None

    @classmethod
    def uncomputable(cls, last_verified_at: str | None = None) -> "PageFingerprint":
        """A page we could not fingerprint — the detector treats it as CHANGED."""
        return cls(
            structural_hash="", control_fps=(), computable=False,
            last_verified_at=last_verified_at,
        )

    def to_dict(self) -> dict:
        """Serialise to the ``app_fingerprints.page_fingerprints`` JSONB shape."""
        return {
            "structural_hash": self.structural_hash,
            "control_fps": list(self.control_fps),
            "computable": self.computable,
            "last_verified_at": self.last_verified_at,
        }

    @classmethod
    def from_dict(cls, d: Mapping | None) -> "PageFingerprint":
        """Load from a stored dict; a malformed/empty entry → uncomputable
        (fail-safe: a corrupt baseline row must never read as 'unchanged')."""
        if not isinstance(d, Mapping):
            return cls.uncomputable()
        sh = str(d.get("structural_hash") or "")
        raw_fps = d.get("control_fps") or []
        fps = tuple(sorted({str(f) for f in raw_fps if f})) if isinstance(raw_fps, (list, tuple)) else ()
        computable = bool(d.get("computable", True)) and bool(sh)
        return cls(
            structural_hash=sh, control_fps=fps, computable=computable,
            last_verified_at=(d.get("last_verified_at") or None),
        )


@dataclass(frozen=True)
class FingerprintSnapshot:
    """A whole app's per-page fingerprints at one point in time.

    ``available`` is the single fail-safe flag: True only when the source graph
    was fetched AND parsed into ≥1 computable page.  When False, the app graph is
    UNCOMPUTABLE as a whole and the detector re-runs everything (never trusts an
    incremental result).
    """

    available: bool
    pages: Mapping[str, PageFingerprint] = field(default_factory=dict)
    source: str = ""
    graph_hash: str = ""
    error: str = ""

    # ── constructors ──
    @classmethod
    def unavailable(cls, *, source: str = "unavailable", error: str = "") -> "FingerprintSnapshot":
        """The graph could not be computed at all (fail-safe sentinel)."""
        return cls(available=False, pages={}, source=source, error=error)

    @classmethod
    def from_page_fingerprints(
        cls, mapping: Mapping | None, *, source: str = "baseline",
    ) -> "FingerprintSnapshot":
        """Load a baseline from an ``app_fingerprints.page_fingerprints`` dict.

        An empty/missing mapping yields an ``available`` snapshot with zero pages
        (a first-ever baseline: the detector then treats every LIVE page as new).
        """
        pages: dict[str, PageFingerprint] = {}
        for pk, entry in (mapping or {}).items():
            key = str(pk)
            if key:
                pages[key] = PageFingerprint.from_dict(entry)
        return cls(
            available=True, pages=pages, source=source,
            graph_hash=_graph_hash(pages),
        )

    # ── accessors ──
    def page_keys(self) -> frozenset[str]:
        return frozenset(self.pages.keys())

    def get(self, page_key: str) -> PageFingerprint | None:
        """Look up a page.

          * graph unavailable            → an ``uncomputable`` page (→ CHANGED);
          * graph available, page present → its fingerprint;
          * graph available, page absent  → ``None`` (a *vanished* page).
        """
        if not self.available:
            return PageFingerprint.uncomputable()
        return self.pages.get(normalize_page_key(page_key))

    def to_page_fingerprints(self) -> dict:
        """Serialise to the JSONB shape stored in ``app_fingerprints``."""
        return {pk: fp.to_dict() for pk, fp in self.pages.items()}


@dataclass(frozen=True)
class FingerprintDiff:
    """The pure page-level diff between two snapshots (baseline vs live)."""

    changed_pages: frozenset[str]
    changed_control_fps: frozenset[str]
    vanished_pages: frozenset[str]        # possible deletions (positive evidence)
    uncomputable_pages: frozenset[str]    # treated changed (fail-safe)
    unchanged_pages: frozenset[str]
    live_graph_uncomputable: bool         # the ENTIRE live graph was uncomputable


# ─── Structural hashing ──────────────────────────────────────────────────────

def _canonical_edge(to_page: str, verb: str, control_fp: str) -> tuple[str, str, str]:
    return (normalize_page_key(to_page), _norm(verb), (control_fp or "").strip())


def _structural_hash(edges: list[tuple[str, str, str]]) -> str:
    """sha256 over the page's canonicalised, sorted outgoing-edge set.

    A page with zero outgoing edges hashes to a stable 'leaf' value — legitimate
    and comparable (a leaf that later grows an edge flips the hash → CHANGED).
    """
    payload = json.dumps(sorted(edges), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _graph_hash(pages: Mapping[str, PageFingerprint]) -> str:
    payload = json.dumps(
        sorted((pk, fp.structural_hash) for pk, fp in pages.items()),
        separators=(",", ":"), ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── Journey-graph parsing (defensive; fail-safe → unavailable) ──────────────

def _first(d: Mapping, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = d.get(k)
        if v:
            return str(v)
    return ""


def _extract_list(graph: Mapping, keys: tuple[str, ...]) -> list:
    for k in keys:
        v = graph.get(k)
        if isinstance(v, list):
            return v
    return []


def parse_journey_graph(graph: Mapping | None, *, source: str = "journey_graph") -> FingerprintSnapshot:
    """Parse a journey-graph response into a :class:`FingerprintSnapshot`.

    Defensive by design (the endpoint's exact output is VM-only): recognises the
    common node/edge spellings, keys pages by :func:`normalize_page_key`, and
    derives each page's structural hash + navigating-control fingerprints from its
    OUTGOING edges.  Any control missing an explicit ``control_fp`` is re-derived
    from its label/verb (:func:`derive_control_fp`); an unlabelled control simply
    contributes no fingerprint (still counted in the structural hash via its
    ``to_page``/``verb``, so a change there is never hidden).

    FAIL-SAFE: if the body is not a mapping, or parses to ZERO computable pages
    (empty or unrecognisable), returns :meth:`FingerprintSnapshot.unavailable` —
    the detector then re-runs everything rather than trust an empty result.
    """
    if not isinstance(graph, Mapping):
        return FingerprintSnapshot.unavailable(
            source=source, error="journey-graph body is not a JSON object",
        )

    nodes = _extract_list(graph, _NODE_KEYS)
    edges = _extract_list(graph, _EDGE_KEYS)

    # Gather outgoing edges per from_page (that is where the control lives).
    outgoing: dict[str, list[tuple[str, str, str]]] = {}
    fps_by_page: dict[str, set[str]] = {}
    for e in edges:
        if not isinstance(e, Mapping):
            continue
        frm = normalize_page_key(_first(e, _EDGE_FROM_FIELDS))
        if not frm:
            continue
        to = _first(e, _EDGE_TO_FIELDS)
        verb = str(e.get("verb") or "")
        fp = str(e.get("control_fp") or "").strip()
        if not fp:
            fp = derive_control_fp(e.get("control_label"), e.get("kind"), frm)
        outgoing.setdefault(frm, []).append(_canonical_edge(to, verb, fp))
        if fp:
            fps_by_page.setdefault(frm, set()).add(fp)

    # Page keys come from explicit nodes AND from any edge endpoint (so a
    # transition target that has no node row still becomes a comparable page).
    page_keys: set[str] = set()
    for n in nodes:
        if isinstance(n, Mapping):
            pk = normalize_page_key(_first(n, _PAGE_KEY_FIELDS))
            if pk:
                page_keys.add(pk)
    page_keys |= set(outgoing.keys())
    for e in edges:
        if isinstance(e, Mapping):
            to = normalize_page_key(_first(e, _EDGE_TO_FIELDS))
            if to:
                page_keys.add(to)

    pages: dict[str, PageFingerprint] = {}
    for pk in page_keys:
        edge_set = outgoing.get(pk, [])
        pages[pk] = PageFingerprint(
            structural_hash=_structural_hash(edge_set),
            control_fps=tuple(sorted(fps_by_page.get(pk, set()))),
            computable=True,
        )

    if not pages:
        return FingerprintSnapshot.unavailable(
            source=f"{source}_empty",
            error="journey-graph produced zero computable pages",
        )
    return FingerprintSnapshot(
        available=True, pages=pages, source=source, graph_hash=_graph_hash(pages),
    )


# ─── Live probe (async; the ONLY VM-deferred seam) ───────────────────────────

async def fetch_journey_graph(
    *, tenant_id: str, artifact_id: str, timeout_s: float = 30.0,
) -> dict | None:
    """Fetch ``GET /test-factory/{artifact_id}/journey-graph`` with a service JWT.

    Returns the parsed JSON dict on HTTP 200, else ``None`` on ANY failure
    (transport error, non-200, non-JSON, non-object) so the caller fails SAFE.
    Never raises — an unreachable/absent journey-graph must degrade the cycle to
    a full re-run, never crash the driver loop.
    """
    import httpx  # local import: pure-logic tests never touch the network

    from ...config import settings
    from ...service_token import mint_service_jwt

    try:
        token = mint_service_jwt(tenant_id)
    except ValueError as exc:  # empty tenant — nothing honest to fetch
        logger.warning("qec.fingerprints.mint_failed", extra={"error": str(exc)[:200]})
        return None

    path = f"/api/v1/test-factory/{artifact_id}/journey-graph"
    try:
        async with httpx.AsyncClient(
            base_url=settings.platform_api_url, timeout=timeout_s,
        ) as client:
            resp = await client.get(path, headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:  # transport failure — fail SAFE
        logger.warning(
            "qec.fingerprints.journey_graph_transport_error",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id, "error": str(exc)[:300]},
        )
        return None

    if resp.status_code != 200:
        logger.warning(
            "qec.fingerprints.journey_graph_non_200",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id,
                   "status_code": resp.status_code},
        )
        return None
    try:
        body = resp.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def probe_live_fingerprints(
    *,
    tenant_id: str,
    artifact_id: str,
    fetcher: JourneyGraphFetcher = fetch_journey_graph,
) -> FingerprintSnapshot:
    """Seed a LIVE :class:`FingerprintSnapshot` for ``artifact_id``.

    Calls ``fetcher`` (default :func:`fetch_journey_graph`; injectable for tests)
    and parses the result.  FAIL-SAFE at every step: a ``None`` graph, or any
    parse failure, yields :meth:`FingerprintSnapshot.unavailable` so the detector
    treats the whole app as changed rather than risk a silent green.
    """
    if not artifact_id:
        return FingerprintSnapshot.unavailable(
            source="no_artifact", error="no latest_artifact_id to probe",
        )
    try:
        graph = await fetcher(tenant_id=tenant_id, artifact_id=artifact_id)
    except Exception as exc:  # a mis-behaving fetcher must never kill the cycle
        logger.warning(
            "qec.fingerprints.fetcher_raised",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id, "error": str(exc)[:300]},
        )
        return FingerprintSnapshot.unavailable(
            source="fetcher_error", error=f"fetcher raised: {exc}"[:300],
        )
    if graph is None:
        return FingerprintSnapshot.unavailable(
            source="journey_graph_unavailable",
            error="journey-graph endpoint unavailable (journey_graph.py absent / non-200)",
        )
    try:
        return parse_journey_graph(graph)
    except Exception as exc:  # unexpected shape — still fail SAFE
        logger.warning(
            "qec.fingerprints.parse_error",
            extra={"tenant_id": tenant_id, "artifact_id": artifact_id, "error": str(exc)[:300]},
        )
        return FingerprintSnapshot.unavailable(
            source="unparseable", error=f"unparseable journey-graph: {exc}"[:300],
        )


# ─── Pure snapshot diff (the heart; operates on already-fetched fingerprints) ─

def diff_snapshots(
    baseline: FingerprintSnapshot, live: FingerprintSnapshot,
) -> FingerprintDiff:
    """Diff a baseline snapshot against a freshly-probed live snapshot.

    FAIL-SAFE-TO-CHANGED:
      * live graph UNAVAILABLE          → every baseline page CHANGED +
                                          ``uncomputable`` (never ``vanished`` —
                                          we cannot confirm deletion when we
                                          computed nothing);
      * live page absent from a computable graph → ``vanished`` (possible
                                          deletion) + CHANGED;
      * live page marked uncomputable   → ``uncomputable`` + CHANGED;
      * page new in live / structural or control-set delta → CHANGED;
      * only identical structural_hash AND identical control_fps → UNCHANGED.
    """
    if not live.available:
        base_keys = baseline.page_keys()
        all_base_fps = frozenset(
            fp for p in baseline.pages.values() for fp in p.control_fps
        )
        return FingerprintDiff(
            changed_pages=base_keys,
            changed_control_fps=all_base_fps,
            vanished_pages=frozenset(),
            uncomputable_pages=base_keys,
            unchanged_pages=frozenset(),
            live_graph_uncomputable=True,
        )

    changed: set[str] = set()
    vanished: set[str] = set()
    uncomputable: set[str] = set()
    unchanged: set[str] = set()
    changed_fps: set[str] = set()

    for pk in baseline.page_keys() | live.page_keys():
        b = baseline.pages.get(pk)
        lv = live.pages.get(pk)

        if lv is None:
            # In baseline, absent from an OTHERWISE-computable live graph.
            vanished.add(pk)
            changed.add(pk)
            if b:
                changed_fps |= set(b.control_fps)
        elif not lv.computable:
            uncomputable.add(pk)
            changed.add(pk)
            changed_fps |= set(lv.control_fps) | (set(b.control_fps) if b else set())
        elif b is None or not b.computable:
            # New page (or an uncomputable baseline entry) → CHANGED.
            changed.add(pk)
            changed_fps |= set(lv.control_fps)
        elif (
            b.structural_hash != lv.structural_hash
            or set(b.control_fps) != set(lv.control_fps)
        ):
            changed.add(pk)
            if b.structural_hash != lv.structural_hash:
                # A topology change invalidates every control on the page.
                changed_fps |= set(b.control_fps) | set(lv.control_fps)
            else:
                changed_fps |= set(b.control_fps) ^ set(lv.control_fps)
        else:
            unchanged.add(pk)

    return FingerprintDiff(
        changed_pages=frozenset(changed),
        changed_control_fps=frozenset(changed_fps),
        vanished_pages=frozenset(vanished),
        uncomputable_pages=frozenset(uncomputable),
        unchanged_pages=frozenset(unchanged),
        live_graph_uncomputable=False,
    )


__all__ = [
    "PageFingerprint",
    "FingerprintSnapshot",
    "FingerprintDiff",
    "JourneyGraphFetcher",
    "normalize_page_key",
    "derive_control_fp",
    "parse_journey_graph",
    "fetch_journey_graph",
    "probe_live_fingerprints",
    "diff_snapshots",
]
