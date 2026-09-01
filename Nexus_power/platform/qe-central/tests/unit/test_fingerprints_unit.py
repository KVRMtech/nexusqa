"""QE-Central S5 — fingerprint store unit tests (pure logic + injected fetcher).

Proves the FAIL-SAFE contract of the journey-graph seam (design §3.5 / open
decision #6) WITHOUT any network or the VM-only ``journey_graph.py``:

  * a parseable graph → per-page structural fingerprints + navigating-control fps;
  * an unavailable / empty / non-object graph → ``unavailable`` (never a false
    "no change");
  * an injected fetcher returning None / raising → ``unavailable`` (fail-safe);
  * ``diff_snapshots`` treats uncomputable pages and a whole-unavailable live
    graph as CHANGED, and a vanished page as a ``possible_deletion``.
"""
from __future__ import annotations

import pytest

from app.controlplane.cycle import fingerprints as F
from app.controlplane.cycle.fingerprints import (
    FingerprintSnapshot,
    PageFingerprint,
    derive_control_fp,
    diff_snapshots,
    normalize_page_key,
    parse_journey_graph,
    probe_live_fingerprints,
)


# ── helpers ───────────────────────────────────────────────────────────────
def _edge(frm, to, *, fp="", label="", verb="click", kind=""):
    e = {"from_page": frm, "to_page": to, "verb": verb}
    if fp:
        e["control_fp"] = fp
    if label:
        e["control_label"] = label
    if kind:
        e["kind"] = kind
    return e


def _graph(nodes, edges, *, node_key="nodes", edge_key="edges"):
    return {
        node_key: [{"page_key": n} for n in nodes],
        edge_key: list(edges),
    }


# ── normalisation / fingerprint derivation ──────────────────────────────────
def test_normalize_page_key_mirrors_control_ledger_algorithm():
    assert normalize_page_key("https://host/transfer") == "/transfer"
    assert normalize_page_key("https://host/checkout/step-2?x=1#h") == "/checkout/step-2"
    assert normalize_page_key("/form") == "/form"
    assert normalize_page_key("https://host/") == "/"
    assert normalize_page_key("") == ""
    assert normalize_page_key(None) == ""


def test_derive_control_fp_is_deterministic_and_empty_on_no_label():
    a = derive_control_fp("Continue", "button", "/login")
    b = derive_control_fp("  continue ", "BUTTON", "https://host/login")
    assert a == b, "normalisation must make label/kind/page casing irrelevant"
    assert len(a) == 40
    assert derive_control_fp("", "button", "/login") == ""
    assert derive_control_fp(None) == ""


def test_page_fingerprint_dict_round_trip():
    fp = PageFingerprint(structural_hash="abc", control_fps=("z", "a"), last_verified_at="2026-01-01T00:00:00Z")
    back = PageFingerprint.from_dict(fp.to_dict())
    assert back.structural_hash == "abc"
    assert set(back.control_fps) == {"a", "z"}
    assert back.computable is True


def test_from_dict_malformed_is_uncomputable_fail_safe():
    assert PageFingerprint.from_dict(None).computable is False
    assert PageFingerprint.from_dict({}).computable is False  # no structural_hash
    assert PageFingerprint.from_dict({"structural_hash": ""}).computable is False


# ── parse_journey_graph ──────────────────────────────────────────────────────
def test_parse_builds_pages_with_structural_hash_and_control_fps():
    g = _graph(
        nodes=["/login", "/form", "/confirm"],
        edges=[
            _edge("/login", "/form", fp="FP_CONTINUE"),
            _edge("/form", "/confirm", fp="FP_SUBMIT"),
        ],
    )
    snap = parse_journey_graph(g)
    assert snap.available is True
    assert snap.page_keys() == frozenset({"/login", "/form", "/confirm"})
    assert snap.pages["/login"].control_fps == ("FP_CONTINUE",)
    assert snap.pages["/form"].control_fps == ("FP_SUBMIT",)
    # a leaf page (no outgoing edges) is computable with a stable structural hash
    assert snap.pages["/confirm"].computable is True
    assert snap.pages["/confirm"].control_fps == ()


def test_parse_accepts_pages_transitions_spelling():
    g = _graph(
        nodes=["/a", "/b"],
        edges=[_edge("/a", "/b", fp="FP")],
        node_key="pages", edge_key="transitions",
    )
    snap = parse_journey_graph(g)
    assert snap.available is True
    assert "/a" in snap.pages and "/b" in snap.pages


def test_parse_derives_control_fp_from_label_when_absent():
    g = _graph(nodes=["/a", "/b"], edges=[_edge("/a", "/b", label="Continue", kind="button")])
    snap = parse_journey_graph(g)
    expected = derive_control_fp("Continue", "button", "/a")
    assert snap.pages["/a"].control_fps == (expected,)


def test_parse_empty_graph_is_unavailable_fail_safe():
    assert parse_journey_graph({"nodes": [], "edges": []}).available is False
    assert parse_journey_graph({}).available is False


def test_parse_non_object_is_unavailable_fail_safe():
    assert parse_journey_graph(None).available is False
    assert parse_journey_graph([1, 2, 3]).available is False  # type: ignore[arg-type]
    assert parse_journey_graph("garbage").available is False  # type: ignore[arg-type]


def test_from_page_fingerprints_round_trips_via_to_page_fingerprints():
    g = _graph(nodes=["/a", "/b"], edges=[_edge("/a", "/b", fp="FP")])
    snap = parse_journey_graph(g)
    reloaded = FingerprintSnapshot.from_page_fingerprints(snap.to_page_fingerprints())
    assert reloaded.available is True
    assert reloaded.pages["/a"].structural_hash == snap.pages["/a"].structural_hash
    assert reloaded.pages["/a"].control_fps == snap.pages["/a"].control_fps


# ── probe_live_fingerprints (injected fetcher; NO network) ───────────────────
@pytest.mark.asyncio
async def test_probe_available_when_fetcher_returns_graph():
    g = _graph(nodes=["/a", "/b"], edges=[_edge("/a", "/b", fp="FP")])

    async def fetcher(*, tenant_id, artifact_id):
        return g

    snap = await probe_live_fingerprints(tenant_id="t1", artifact_id="art1", fetcher=fetcher)
    assert snap.available is True
    assert "/a" in snap.pages


@pytest.mark.asyncio
async def test_probe_unavailable_when_fetcher_returns_none():
    async def fetcher(*, tenant_id, artifact_id):
        return None  # endpoint down / journey_graph.py absent

    snap = await probe_live_fingerprints(tenant_id="t1", artifact_id="art1", fetcher=fetcher)
    assert snap.available is False
    assert "unavailable" in snap.source


@pytest.mark.asyncio
async def test_probe_unavailable_when_fetcher_raises():
    async def fetcher(*, tenant_id, artifact_id):
        raise RuntimeError("boom")

    snap = await probe_live_fingerprints(tenant_id="t1", artifact_id="art1", fetcher=fetcher)
    assert snap.available is False  # a mis-behaving fetcher never crashes the cycle


@pytest.mark.asyncio
async def test_probe_unavailable_without_artifact_id():
    async def fetcher(*, tenant_id, artifact_id):  # pragma: no cover - must not be called
        raise AssertionError("fetcher must not be called without an artifact_id")

    snap = await probe_live_fingerprints(tenant_id="t1", artifact_id="", fetcher=fetcher)
    assert snap.available is False


# ── diff_snapshots ───────────────────────────────────────────────────────────
def _snap(pages):
    return FingerprintSnapshot(available=True, pages=pages, source="synthetic")


def test_diff_identical_snapshots_report_no_change():
    base = parse_journey_graph(_graph(["/a", "/b"], [_edge("/a", "/b", fp="FP")]))
    live = parse_journey_graph(_graph(["/a", "/b"], [_edge("/a", "/b", fp="FP")]))
    d = diff_snapshots(base, live)
    assert d.changed_pages == frozenset()
    assert d.unchanged_pages == frozenset({"/a", "/b"})
    assert d.live_graph_uncomputable is False


def test_diff_structural_change_marks_page_changed():
    base = parse_journey_graph(_graph(["/a", "/b"], [_edge("/a", "/b", fp="FP_OLD")]))
    live = parse_journey_graph(_graph(["/a", "/b"], [_edge("/a", "/b", fp="FP_NEW")]))
    d = diff_snapshots(base, live)
    assert "/a" in d.changed_pages
    # a topology change invalidates every control on the page (fail-safe)
    assert {"FP_OLD", "FP_NEW"} <= d.changed_control_fps


def test_diff_vanished_page_is_possible_deletion_and_changed():
    base = parse_journey_graph(_graph(["/hub", "/a", "/b"], [
        _edge("/hub", "/a", fp="FA"), _edge("/hub", "/b", fp="FB"),
    ]))
    live = parse_journey_graph(_graph(["/hub", "/a"], [_edge("/hub", "/a", fp="FA")]))
    d = diff_snapshots(base, live)
    assert "/b" in d.vanished_pages
    assert "/b" in d.changed_pages
    assert "/b" not in d.uncomputable_pages  # positive evidence of deletion, not uncertainty


def test_diff_live_unavailable_treats_all_baseline_pages_changed_never_vanished():
    base = parse_journey_graph(_graph(["/a", "/b"], [_edge("/a", "/b", fp="FP")]))
    live = FingerprintSnapshot.unavailable()
    d = diff_snapshots(base, live)
    assert d.live_graph_uncomputable is True
    assert d.changed_pages == frozenset({"/a", "/b"})
    assert d.uncomputable_pages == frozenset({"/a", "/b"})
    assert d.vanished_pages == frozenset()  # cannot confirm deletion when we computed nothing


def test_diff_single_uncomputable_live_page_is_changed():
    base = _snap({"/a": PageFingerprint(structural_hash="h", control_fps=("FA",))})
    live = _snap({"/a": PageFingerprint.uncomputable()})
    d = diff_snapshots(base, live)
    assert "/a" in d.changed_pages
    assert "/a" in d.uncomputable_pages


def test_diff_new_live_page_is_changed():
    base = _snap({"/a": PageFingerprint(structural_hash="h", control_fps=())})
    live = _snap({
        "/a": PageFingerprint(structural_hash="h", control_fps=()),
        "/new": PageFingerprint(structural_hash="h2", control_fps=("FN",)),
    })
    d = diff_snapshots(base, live)
    assert "/new" in d.changed_pages
    assert "/a" in d.unchanged_pages
