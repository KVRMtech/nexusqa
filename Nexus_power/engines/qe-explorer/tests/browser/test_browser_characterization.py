"""T-HN-03 — CHARACTERIZATION (golden snapshot) framework.

    Fixture App → Run Crawl → Generate Manifest → Normalize → Golden → byte-compare

The pipeline is the PRODUCTION one end to end:

  * the browser is real headless Chromium,
  * the port is ``app.main.PlaywrightBrowserPort`` — the class the crawl
    entrypoint constructs,
  * the driver is ``app.crawler.Crawler`` — the real state machine, real budget
    accounting, real fail-closed guard, real refuse pack,
  * the manifest is written by ``app.emit.ManifestEmitter`` to a real
    ``manifest.jsonl`` on disk.

Nothing between the fixture HTML and the golden file is a test double.

## What normalisation may touch

ONLY values that legitimately differ between two identical runs: minted ids
(crawl_id, state_id), clock readings (``*_ms``), per-run filesystem paths, and
the ephemeral fixture-server port. Everything functional — names, roles,
options, group keys, outcomes, coverage, counts, ORDER — passes through
untouched, because a characterization test that normalised behaviour would pass
through the very change it exists to catch.

``test_a_behavioural_change_breaks_the_golden`` proves this property rather than
asserting it: it mutates the production walker in memory, re-runs the crawl, and
requires a diff.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.characterization, pytest.mark.playwright]


# ─── Building a real crawl ───────────────────────────────────────────────────

def _run_real_crawl(pw, url: str, work_dir: Path, *, crawl_id: str) -> dict[str, Any]:
    """Drive the PRODUCTION Crawler over ``url`` and return its manifest records.

    Mirrors ``app.main._run_job``'s construction: same GuardContext, same
    RefusePack loaded from the shipped ``refuse_pack.yaml``, same version
    stamps. The budget is small and explicit — a characterization crawl must
    terminate on a *declared* bound, never on a timeout, or the golden would
    encode how fast the machine happened to be.
    """
    from app.crawler import Budget, Crawler, GuardContext
    from app.auth import AuthWindow
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort

    pack = load_refuse_pack(str(Path(__file__).resolve().parents[2] / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=50, window_ms=60_000),
        attestation=None,
        submit_flow_approved=False,
        idp_domains=frozenset(),
    )
    # Deterministic, declared bounds. `max_states=1` keeps the crawl on the
    # fixture page under test: these fixtures isolate CAPTURE, and a wandering
    # frontier would fold unrelated pages into the golden.
    budget = Budget.from_dict({
        "max_states": 1, "max_actions": 0, "max_requests": 200,
        "max_duration_ms": 120_000,
    })

    # A FRESH context + page per crawl (M1.5).  Every characterization crawl used
    # to share the session page, which meant each new port stacked another set of
    # listeners on it: by fixture 18 a single response was being recorded
    # eighteen times, and a popup opened by one fixture was still counted by the
    # next fixture's port.  That was invisible while the `response` listener
    # silently failed to attach in this lane at all; repairing the attachment
    # (see PlaywrightBrowserPort._attach_page_observers) made it observable, as a
    # page token that drifted between two runs of the same fixture.  One crawl,
    # one browser context — which is also what production does.
    context = pw.run(pw.fresh_context())
    page = pw.run(context.new_page())
    port = PlaywrightBrowserPort(page, context)
    crawler = Crawler(
        port,
        crawl_id=crawl_id,
        tenant_id="characterization",
        target_url=url,
        work_dir=str(work_dir),
        refuse_pack=pack,
        budget=budget,
        explorer_version=EXPLORER_VERSION,
        guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint="characterization-fixed",
        guard_context=guard_ctx,
        # A fixed identity seed: `derive_identity` is seeded, so the same seed
        # yields the same fictional person every run. Without pinning it, every
        # synthesized value would differ and no golden could ever be stable.
        identity_seed="qec-characterization",
        observe_only=True,          # capture only — never mutate a fixture app
    )
    try:
        pw.run(crawler.run())
    finally:
        try:
            pw.run(context.close())
        except Exception:
            pass

    manifest = work_dir / crawl_id / "manifest.jsonl"
    assert manifest.exists(), f"no manifest was written at {manifest}"
    records = [json.loads(line) for line in
               manifest.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert records, "the manifest is empty — the crawl produced no records"
    return {"records": records, "manifest_path": manifest}


def _snapshot(pw, fixture_server, fixture_name: str, work_dir: Path,
              *, crawl_id: str | None = None) -> list[dict[str, Any]]:
    """One crawl → one normalised, byte-stable snapshot."""
    out = _run_real_crawl(
        pw, fixture_server.url(fixture_name), work_dir,
        crawl_id=crawl_id or f"char-{fixture_name}")
    return H.normalize_manifest(out["records"])


@pytest.fixture(scope="session")
def char_work_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("characterization")


# ─── The goldens ─────────────────────────────────────────────────────────────

def test_manifest_golden(pw, fixture_server, fixture_name, char_work_dir) -> None:
    """Byte-compare this fixture's normalised manifest against its golden.

    A behavioural change in Capture — a renamed field, a dropped option, a
    different group_key, a lost control, a changed coverage row — produces a
    unified diff and a red test.
    """
    snapshot = _snapshot(pw, fixture_server, fixture_name, char_work_dir)
    H.assert_golden(f"manifest_{fixture_name}", snapshot)


def test_inventory_golden(pw, fixture_server, fixture_name) -> None:
    """Byte-compare the COMPLETE captured control array against its golden.

    The manifest is a deliberate, lossy PROJECTION of the capture: it carries
    ``form_snapshot_signals`` rather than raw controls, and it re-truncates
    option lists (see BUG-CATALOG-TRUNCATION-60). A manifest-only golden would
    therefore be blind to a whole class of capture regressions — a dropped
    `pattern`, a changed `name_source`, a lost `options_total`.

    This golden snapshots what the production port actually returned, so every
    field of every RawControl is under byte-for-byte characterization.
    """
    spec = H.fixture_spec(fixture_name)
    if "playwright" not in spec.get("lanes", []):
        pytest.skip(f"{fixture_name} is declared {spec['lanes']}-only")

    controls = pw.collect(fixture_server.url(fixture_name))
    H.assert_golden(f"inventory_{fixture_name}", H.normalize_value(controls))


def test_golden_covers_every_fixture() -> None:
    """No fixture may be missing a recorded golden.

    Without this, deleting a golden would silently downgrade its fixture from
    "byte-compared" to "recorded on next run" and the safety net would develop a
    hole nothing reports.
    """
    # QEC_UPDATE_GOLDENS deliberately DISABLES the comparison this test depends
    # on - `assert_golden` records instead of raising - so the test cannot hold
    # in that mode. Skipping is the honest outcome; failing would train people to
    # ignore a red recording run, which is exactly when they should be reading
    # the diff most carefully.
    if H.UPDATE_GOLDENS:
        pytest.skip("goldens are being (re)recorded; the comparison is disabled")
    missing_manifest = [n for n in H.fixture_names()
                        if not H.golden_path(f"manifest_{n}").exists()]
    missing_inventory = [
        n for n in H.fixture_names()
        if "playwright" in H.fixture_spec(n).get("lanes", [])
        and not H.golden_path(f"inventory_{n}").exists()
    ]
    assert not missing_manifest and not missing_inventory, (
        f"fixtures with no recorded golden — manifest: {missing_manifest}, "
        f"inventory: {missing_inventory}. Record them with "
        f"QEC_UPDATE_GOLDENS=1 python -m pytest tests/browser/test_browser_characterization.py")


# ─── Determinism ─────────────────────────────────────────────────────────────

def test_characterization_is_deterministic(pw, fixture_server, char_work_dir) -> None:
    """Two full crawls of the same fixture normalise to identical bytes.

    Run on the busiest fixture. Two DIFFERENT crawl_ids are used deliberately:
    if normalisation were leaking a minted id or a clock reading, this is where
    it surfaces — as a reproducible failure rather than as an intermittent
    golden diff months from now.
    """
    a = _snapshot(pw, fixture_server, "07-native-select-250", char_work_dir,
                  crawl_id=f"det-a-{uuid.uuid4().hex[:8]}")
    b = _snapshot(pw, fixture_server, "07-native-select-250", char_work_dir,
                  crawl_id=f"det-b-{uuid.uuid4().hex[:8]}")
    assert H.canonical_json(a) == H.canonical_json(b), (
        "two identical crawls produced different normalised manifests — either "
        "capture is nondeterministic or normalisation is leaking a run-varying "
        "value")


def test_normalisation_does_not_erase_behaviour() -> None:
    """Normalisation must be narrow enough to let a real change through.

    Guards the framework's central risk: over-normalise and every
    characterization test passes forever while behaviour drifts underneath.
    """
    before = [{
        "type": "page_state", "crawl_id": "abc-123", "ts_ms": 1000,
        "controls": [{"name": "Continue", "role": "button", "options": ["A", "B"],
                      "group_key": "name:f:q1"}],
    }]
    # A functional change of every kind the walker can express.
    mutations = [
        ("a renamed control", lambda r: r[0]["controls"][0].__setitem__("name", "Proceed")),
        ("a changed role", lambda r: r[0]["controls"][0].__setitem__("role", "link")),
        ("a dropped option", lambda r: r[0]["controls"][0].__setitem__("options", ["A"])),
        ("a regrouped question", lambda r: r[0]["controls"][0].__setitem__("group_key", "")),
        ("a lost control", lambda r: r[0].__setitem__("controls", [])),
        ("a changed record type", lambda r: r[0].__setitem__("type", "action")),
    ]
    base = H.canonical_json(H.normalize_manifest(before))
    for label, mutate in mutations:
        after = json.loads(json.dumps(before))
        mutate(after)
        assert H.canonical_json(H.normalize_manifest(after)) != base, (
            f"normalisation erased {label} — a real behavioural change would "
            f"pass through the characterization suite unnoticed")

    # And the converse: run-varying values MUST be erased, or every run is a diff.
    noisy = json.loads(json.dumps(before))
    noisy[0]["crawl_id"] = "zzz-999"
    noisy[0]["ts_ms"] = 987654
    assert H.canonical_json(H.normalize_manifest(noisy)) == base, (
        "normalisation left a minted id or a clock reading in place — every run "
        "would diff against its own golden")


def test_declared_bounds_are_not_normalised() -> None:
    """A configured budget is functional output and must survive normalisation.

    The manifest holds both clock READINGS (`first_seen_ms`, `elapsed_ms`) and
    declared BOUNDS (`max_wall_ms`). They are the same *shape* and opposite in
    kind. Normalising by suffix would erase the bound, and a crawl silently
    reconfigured to a smaller budget — which changes how much of an application
    is ever seen — would pass its own golden unchanged.
    """
    reading = [{"type": "page_state", "first_seen_ms": 100, "last_seen_ms": 200}]
    assert H.canonical_json(H.normalize_manifest(reading)) == \
        H.canonical_json(H.normalize_manifest(
            [{"type": "page_state", "first_seen_ms": 999, "last_seen_ms": 888}])), \
        "clock readings must normalise away"

    bound_a = [{"type": "crawl_meta", "budgets": {"max_wall_ms": 120_000}}]
    bound_b = [{"type": "crawl_meta", "budgets": {"max_wall_ms": 5_000}}]
    assert H.canonical_json(H.normalize_manifest(bound_a)) != \
        H.canonical_json(H.normalize_manifest(bound_b)), (
        "the configured crawl budget was normalised away — a reconfigured crawl "
        "would pass its own golden unchanged")

    assert not (H._UNSTABLE_KEYS & H._DELIBERATELY_NOT_NORMALIZED), (
        "a key is listed as both unstable and deliberately-preserved")


def test_normalisation_preserves_record_order() -> None:
    """Order is functional output: it is the sequence the crawl observed."""
    records = [{"type": "crawl_meta", "n": 1}, {"type": "page_state", "n": 2},
               {"type": "action", "n": 3}]
    assert [r["n"] for r in H.normalize_manifest(records)] == [1, 2, 3]
    assert [r["n"] for r in H.normalize_manifest(list(reversed(records)))] == [3, 2, 1]


# ─── The proof that the net actually catches things ─────────────────────────

def test_a_behavioural_change_breaks_the_golden(pw, fixture_server, char_work_dir,
                                                 monkeypatch) -> None:
    """Mutate Capture → require a characterization diff → revert → require green.

    This is the milestone's load-bearing claim, executed rather than asserted.

    The mutation is applied to the PRODUCTION constant in memory
    (``app.inventory_js.INVENTORY_JS``) via monkeypatch, so the real injection
    path carries it and ``monkeypatch`` guarantees the revert even on failure.
    The edit is deliberately small and semantic — one extra accessible-name rung
    ordering change — exactly the class of change that a string-assertion test
    suite would wave straight through.
    """
    # QEC_UPDATE_GOLDENS deliberately DISABLES the comparison this test depends
    # on - `assert_golden` records instead of raising - so the test cannot hold
    # in that mode. Skipping is the honest outcome; failing would train people to
    # ignore a red recording run, which is exactly when they should be reading
    # the diff most carefully.
    if H.UPDATE_GOLDENS:
        pytest.skip("goldens are being (re)recorded; the comparison is disabled")
    import importlib
    import inspect as _inspect

    import app.inventory_js as ijs
    from app.main import PlaywrightBrowserPort

    # WHERE the injected constant is actually bound. `collect_controls` does
    # `page.evaluate(INVENTORY_JS)` against a module-level name imported with
    # `from .inventory_js import INVENTORY_JS`, which binds a SEPARATE reference
    # at import time — patching `app.inventory_js` alone would not reach it.
    # Discovered rather than hardcoded: this module was `app.main` until the port
    # was extracted into `app.playwright_port`, and a hardcoded name would have
    # turned this proof into a silent no-op the day that happened.
    port_module = importlib.import_module(PlaywrightBrowserPort.__module__)
    assert hasattr(port_module, "INVENTORY_JS"), (
        f"{port_module.__name__} does not bind INVENTORY_JS — find the module "
        f"that does, or this mutation cannot reach the injection path")

    fixture = "10-save-draft-wizard"
    golden_name = f"manifest_{fixture}"

    # 1. BASELINE — must already match its golden.
    baseline = _snapshot(pw, fixture_server, fixture, char_work_dir,
                         crawl_id="mutation-baseline")
    H.assert_golden(golden_name, baseline)

    # 2. MUTATE. Swap the `placeholder` rung above the `label[for]` rung, so a
    #    field with both is named by its placeholder instead of its label. A
    #    real regression of exactly this shape would flip `best_effort` to true
    #    across the fleet and change every generated locator.
    original = ijs.INVENTORY_JS
    mutated = original.replace(
        '    var ph = norm(attr(el, "placeholder"));\n'
        '    if (ph) return { name: ph, source: "placeholder" };\n',
        "",
    ).replace(
        "    // 1. <label for=id>\n",
        '    // 1. <label for=id>\n'
        '    var __ph = norm(attr(el, "placeholder"));\n'
        '    if (__ph) return { name: __ph, source: "placeholder" };\n',
    )
    assert mutated != original, "the mutation did not apply — the test would be vacuous"

    monkeypatch.setattr(ijs, "INVENTORY_JS", mutated)
    monkeypatch.setattr(port_module, "INVENTORY_JS", mutated)

    changed = _snapshot(pw, fixture_server, fixture, char_work_dir,
                        crawl_id="mutation-applied")
    assert H.canonical_json(changed) != H.canonical_json(baseline), (
        "a capture behaviour change produced an IDENTICAL normalised manifest — "
        "the characterization suite cannot detect behavioural change and is "
        "providing no safety net at all")

    with pytest.raises(AssertionError, match="CHARACTERIZATION DIFF"):
        H.assert_golden(golden_name, changed)

    # 3. REVERT (monkeypatch undoes it at teardown; do it explicitly so the
    #    green-again assertion happens inside this test).
    monkeypatch.setattr(ijs, "INVENTORY_JS", original)
    monkeypatch.setattr(port_module, "INVENTORY_JS", original)

    reverted = _snapshot(pw, fixture_server, fixture, char_work_dir,
                         crawl_id="mutation-reverted")
    assert H.canonical_json(reverted) == H.canonical_json(baseline)
    H.assert_golden(golden_name, reverted)      # green again, unchanged golden


def test_goldens_are_not_silently_rerecorded() -> None:
    """Re-recording must be an explicit, opt-in act.

    If ``assert_golden`` re-recorded on mismatch by default, every behavioural
    change would rewrite its own evidence and the suite would be permanently,
    silently green.
    """
    assert H.UPDATE_GOLDENS is False or os.environ.get("QEC_UPDATE_GOLDENS"), (
        "goldens are in update mode without QEC_UPDATE_GOLDENS being set")
