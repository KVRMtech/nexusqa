"""QE-Central — explorer-manifest → substrate-writer CONTRACT TEST (design §7.10).

THE cross-subsystem pin that guarantees the contained explorer's output feeds
the Phase-0 substrate writer unchanged.  A hand-authored ``manifest.jsonl``
fixture (``fixtures/golden_3page_manifest.jsonl``), shaped exactly like the REAL
explorer emitter (``engines/qe-explorer/app/emit.py``), is run through the
``internal.py`` manifest mapper and asserted to produce a VALID
:class:`ExplorationBundle` — with the page_state → ``page_visits`` and action →
``page_actions`` field mapping checked FIELD BY FIELD against the columns the
writer stamps.

Runs WITHOUT a browser or a database: the mapper is pure and the screenshot
loader is injected (returns a 1×1 PNG), so every assertion here is deterministic
and offline.  The one optional subprocess test aligns the emit.py record
dataclasses to the schema field sets; it skips gracefully when the explorer
package is not importable in this checkout.

Companion pins that already exist:
  * ``test_substrate_contract.py`` proves bundle → substrate rows → the REAL
    factory loader round-trips and yields PROVEN navigations.  Together they
    close the loop: explorer manifest → bundle → rows → /generate.
"""
from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.clients.manifest_mapper import (
    ACTION_SCHEMA_FIELDS,
    MANIFEST_ONLY_ACTION_FIELDS,
    MANIFEST_ONLY_PAGE_FIELDS,
    PAGE_STATE_SCHEMA_FIELDS,
    ManifestMappingError,
    map_manifest_records_to_bundle,
)
from app.routers.explorations import _EXTRACTOR_VERSION_PREFIX, _extractor_version
from app.substrate.schema import (
    ACTION_VERBS,
    TARGET_KINDS,
    ExplorationBundle,
    RefusalError,
    validate_bundle,
)
from app.substrate.writer import (
    VISIT_SOURCE,
    _evidence_signals,
    redacted_form_snapshot,
)

_CONTRACT_DIR = Path(__file__).resolve().parent
FIXTURE = _CONTRACT_DIR / "fixtures" / "golden_3page_manifest.jsonl"
REPO_ROOT = _CONTRACT_DIR.parents[3]                 # Nexus_power
EXPLORER_ROOT = REPO_ROOT / "engines" / "qe-explorer"

# The 1×1 PNG the injected loader returns for every staged screenshot path.
_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQ"
    "GAhKmMIQAAAABJRU5ErkJggg=="
)
_PNG_BYTES = base64.b64decode(_PNG_B64)


def _png_loader(_path: str) -> bytes:
    return _PNG_BYTES


def _load_records() -> list[dict]:
    with FIXTURE.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _page_state_records(records: list[dict]) -> list[dict]:
    return [r for r in records if r.get("type") == "page_state"]


def _map(records: list[dict]) -> ExplorationBundle:
    return map_manifest_records_to_bundle(records, screenshot_loader=_png_loader)


# ═══════════════════════════════════════════════════════════════════════
# The fixture mirrors the REAL emit.py record shapes
# ═══════════════════════════════════════════════════════════════════════


class TestFixtureIsExplorerShaped:
    def test_fixture_record_inventory(self):
        records = _load_records()
        metas = [r for r in records if r["type"] == "crawl_meta"]
        pages = _page_state_records(records)
        embedded_actions = [a for p in pages for a in p["actions"]]
        embedded_shots = [s for p in pages for s in p["screenshots"]]
        assert len(metas) == 2, "start + terminal crawl_meta (mapper takes the last)"
        assert len(pages) == 3
        assert [p["url_path"] for p in pages] == ["/login", "/apply", "/confirm"]
        assert len(embedded_actions) == 9
        assert len(embedded_shots) == 3
        # audit-only records are present AND must be ignored by the mapper
        assert any(r["type"] == "guard_event" for r in records)
        assert any(r["type"] == "edge" for r in records)

    def test_fixture_actions_carry_manifest_only_routing_fields(self):
        """Real emit.py actions include state_id/to_state/screenshot_after/phase;
        the mapper must STRIP them (the bundle forbids extras)."""
        records = _load_records()
        sample = _page_state_records(records)[0]["actions"][0]
        for f in MANIFEST_ONLY_ACTION_FIELDS:
            assert f in sample, f"fixture action missing manifest-only field {f!r}"
        # phase lives at the TOP LEVEL (never inside qec) — mirrors emit.py
        assert "phase" in sample and "phase" not in sample["qec"]

    def test_fixture_pages_carry_manifest_only_routing_fields(self):
        page = _page_state_records(_load_records())[0]
        for f in MANIFEST_ONLY_PAGE_FIELDS:
            assert f in page, f"fixture page_state missing manifest-only field {f!r}"


# ═══════════════════════════════════════════════════════════════════════
# Mapper → a VALID ExplorationBundle the schema accepts
# ═══════════════════════════════════════════════════════════════════════


class TestManifestMapsToValidBundle:
    def test_maps_to_schema_valid_bundle(self):
        bundle = _map(_load_records())
        assert isinstance(bundle, ExplorationBundle)
        # re-running EVERY evidence rule accepts it (defence in depth)
        assert validate_bundle(bundle).model_dump() == bundle.model_dump()
        assert len(bundle.pages) == 3
        assert bundle.total_actions() == 9

    def test_header_comes_from_the_last_crawl_meta(self):
        bundle = _map(_load_records())
        assert bundle.crawl_id == "expl-3page-manifest-01"
        assert bundle.target_url == "https://portal.golden-fixture.test/login"
        assert bundle.explorer_version == "qe-explorer-v1"
        assert bundle.config_fingerprint.startswith("sha256:")

    def test_frame_count_is_recomputed_not_trusted(self):
        """The terminal crawl_meta declares frame_count=99; the mapper counts
        the screenshots it actually collected (3) — never trusts the header."""
        records = _load_records()
        terminal = [r for r in records if r["type"] == "crawl_meta"][-1]
        assert terminal["frame_count"] == 99
        bundle = _map(records)
        assert bundle.frame_count == 3
        assert sum(len(p.screenshots) for p in bundle.pages) == 3

    def test_screenshots_decode_to_the_loaded_png(self):
        bundle = _map(_load_records())
        for page in bundle.pages:
            assert len(page.screenshots) == 1
            for shot in page.screenshots:
                assert shot.decode() == _PNG_BYTES


# ═══════════════════════════════════════════════════════════════════════
# page_state → page_visits — field by field
# ═══════════════════════════════════════════════════════════════════════


class TestPageStateToPageVisitMapping:
    def test_url_and_window_fields_map_verbatim(self):
        records = _load_records()
        bundle = _map(records)
        pages = _page_state_records(records)
        for visit, page in zip(bundle.pages, pages):
            assert visit.sequence_index == page["sequence_index"]
            assert visit.location == page["location"]
            assert visit.title == page["title"]
            assert visit.url_host == page["url_host"]
            assert visit.url_path == page["url_path"]
            assert visit.url_query == page["url_query"]
            assert visit.canonical_host == page["canonical_host"]
            assert visit.first_seen_ms == page["first_seen_ms"]
            assert visit.last_seen_ms == page["last_seen_ms"]
            # page_visits.duration_ms (writer derives it from the window)
            assert visit.duration_ms == page["last_seen_ms"] - page["first_seen_ms"]
            # page_visits.form_snapshot_signals — copied verbatim
            assert visit.form_snapshot_signals == page["form_snapshot_signals"]

    def test_form_snapshot_maps_through_the_writer_redactor(self):
        """page_state.form_snapshot → page_visits.form_snapshot via the writer's
        redactor: a password-SIGNALLED field is emptied at source and preserved
        empty; a non-secret value passes through."""
        bundle = _map(_load_records())
        login = bundle.pages[0]
        snapshot, counts, errors = redacted_form_snapshot(login)
        assert snapshot["Username"] == "casey.tester"
        assert snapshot["Password"] == ""        # emptied at source (never captured)
        assert errors == 0

    def test_a_raw_password_signal_value_is_redacted_whole(self):
        """Even if a crawler ever committed a raw password value, the
        password SIGNAL forces whole-value redaction into page_visits."""
        bundle = _map(_load_records())
        login = bundle.pages[0]
        login.form_snapshot["Password"] = "raw-secret-must-never-persist"
        snapshot, _counts, _errors = redacted_form_snapshot(login)
        assert snapshot["Password"] == "[REDACTED:password]"

    def test_visit_source_is_the_proven_gate_literal(self):
        # every visit the writer stamps carries source='ground_truth'
        assert VISIT_SOURCE == "ground_truth"

    def test_network_calls_stream_maps_additively(self):
        """API/network mining (#6): a page_state's ``network_calls`` stream rides
        the mapper into the bundle verbatim, and a manifest WITHOUT it still maps
        (defaults to []) — the substrate accepts the new evidence additively."""
        records = _load_records()
        pages = _page_state_records(records)
        # a pre-#6 manifest page (no network_calls) → [] (backward-compatible).
        assert "network_calls" not in pages[0]
        # inject the stream onto the first page as the crawler would.
        pages[0]["network_calls"] = [
            {"method": "GET", "url": "https://app.example/api/quote",
             "has_query": "true", "status": "200", "resource_type": "xhr",
             "request_mime": "", "response_mime": "application/json",
             "response_bytes": "812", "timestamp_ms": "5"},
        ]
        bundle = _map(records)
        assert bundle.pages[0].network_calls[0]["url"] == "https://app.example/api/quote"
        assert bundle.pages[0].network_calls[0]["response_mime"] == "application/json"
        # a sibling page that never carried the stream stays honestly empty.
        assert bundle.pages[1].network_calls == []


# ═══════════════════════════════════════════════════════════════════════
# action → page_actions — field by field (incl. evidence_signals)
# ═══════════════════════════════════════════════════════════════════════


class TestActionToPageActionMapping:
    def test_core_action_fields_and_vocab(self):
        records = _load_records()
        bundle = _map(records)
        pages = _page_state_records(records)
        for visit, page in zip(bundle.pages, pages):
            manifest_actions = {a["subaction_index"]: a for a in page["actions"]}
            assert len(visit.actions) == len(manifest_actions)
            for action in visit.actions:
                m = manifest_actions[action.subaction_index]
                assert action.verb == m["verb"]
                assert action.verb in ACTION_VERBS
                assert action.target_kind == m["target_kind"]
                assert action.target_kind in TARGET_KINDS
                assert action.target_label == m["target_label"]
                # value: None for a plain click, "" for an emptied password,
                # the committed string for a fill — verbatim from the manifest
                assert action.value == m.get("value")

    def test_evidence_signals_bundle_maps_field_by_field(self):
        """action.{after,url_changed,qec,anchor} → page_actions.evidence_signals
        (the exact dict the factory unpacks — writer._evidence_signals)."""
        records = _load_records()
        bundle = _map(records)
        pages = _page_state_records(records)
        for visit, page in zip(bundle.pages, pages):
            manifest_actions = {a["subaction_index"]: a for a in page["actions"]}
            for action in visit.actions:
                m = manifest_actions[action.subaction_index]
                signals = _evidence_signals(action)
                assert signals["after"]["outcome"] == m["after"]["outcome"]
                assert signals["after"]["detail"] == m["after"]["detail"]
                assert signals["after"]["navigated"] == m["after"]["navigated"]
                assert signals["url_changed"] == m["url_changed"]
                # qec = the inventory diagnostics dict, verbatim (NO phase)
                assert signals["qec"] == m["qec"]
                assert "phase" not in signals["qec"]
                # anchor unpacked verbatim when captured
                assert signals["anchor"] == {"label": m["anchor"]["label"],
                                             "kind": m["anchor"]["kind"]}

    def test_manifest_only_fields_are_stripped(self):
        """The bundle action carries EXACTLY the schema fields — state_id /
        to_state / screenshot_after / phase never leak (the bundle forbids
        extras, so a leak would itself fail validation)."""
        bundle = _map(_load_records())
        expected = set(ACTION_SCHEMA_FIELDS)
        for page in bundle.pages:
            for action in page.actions:
                assert set(action.model_dump().keys()) == expected
                for leaked in MANIFEST_ONLY_ACTION_FIELDS:
                    assert leaked not in action.model_dump()

    def test_recorded_navigations_are_grounded_not_inferred(self):
        bundle = _map(_load_records())
        navs = [a for p in bundle.pages for a in p.actions
                if a.after and a.after.outcome == "navigation"]
        assert len(navs) == 3                      # login→apply, apply→confirm, submit
        for a in navs:
            assert a.after.navigated is True
            assert a.url_changed is True


# ═══════════════════════════════════════════════════════════════════════
# Both emit.py mechanisms: embedded actions AND streamed `action` records
# ═══════════════════════════════════════════════════════════════════════


def _to_streamed(records: list[dict]) -> list[dict]:
    """Re-shape the embedded-action fixture into the streamed form: each page's
    actions become separate ``action`` records keyed by state_id."""
    out: list[dict] = []
    for rec in records:
        if rec.get("type") == "page_state":
            page = copy.deepcopy(rec)
            actions = page.pop("actions")
            page["actions"] = []
            out.append(page)
            for a in actions:
                out.append({"type": "action", **copy.deepcopy(a)})
        else:
            out.append(copy.deepcopy(rec))
    return out


class TestEmbeddedAndStreamedActionsAgree:
    def test_streamed_actions_produce_the_identical_bundle(self):
        embedded = _map(_load_records())
        streamed = _map(_to_streamed(_load_records()))
        assert embedded.model_dump() == streamed.model_dump()
        assert streamed.total_actions() == 9


# ═══════════════════════════════════════════════════════════════════════
# Dishonest manifests can NEVER produce a bundle (fail-closed)
# ═══════════════════════════════════════════════════════════════════════


class TestBrokenManifestsRefuse:
    def test_duplicate_sequence_index_refuses(self):
        records = _load_records()
        _page_state_records(records)[2]["sequence_index"] = 1  # dup of /login
        with pytest.raises(RefusalError) as exc:
            _map(records)
        assert "non_monotonic_sequence_index" in str(exc.value)

    def test_missing_after_bundle_refuses(self):
        records = _load_records()
        del _page_state_records(records)[0]["actions"][0]["after"]
        with pytest.raises(RefusalError) as exc:
            _map(records)
        assert "missing_after_bundle" in str(exc.value)

    def test_fill_without_value_refuses(self):
        records = _load_records()
        # a 'type' action whose committed value is absent is broken capture
        del _page_state_records(records)[0]["actions"][0]["value"]
        with pytest.raises(RefusalError) as exc:
            _map(records)
        assert "missing_fill_value" in str(exc.value)

    def test_screenshot_outside_visit_window_refuses(self):
        records = _load_records()
        _page_state_records(records)[0]["screenshots"][0]["timestamp_ms"] = 999999
        with pytest.raises(RefusalError) as exc:
            _map(records)
        assert "screenshot_outside_visit_window" in str(exc.value)

    def test_missing_crawl_meta_refuses(self):
        records = [r for r in _load_records() if r["type"] != "crawl_meta"]
        with pytest.raises(ManifestMappingError):
            _map(records)

    def test_unloadable_screenshot_refuses(self):
        def _boom(_path: str) -> bytes:
            raise FileNotFoundError(_path)

        with pytest.raises(RefusalError) as exc:
            map_manifest_records_to_bundle(_load_records(), screenshot_loader=_boom)
        assert exc.value.reason == "screenshot_missing_data"

    def test_manifest_mapping_error_is_a_refusal_error(self):
        # so the router funnels it into the SAME honest 422 path
        assert issubclass(ManifestMappingError, RefusalError)


# ═══════════════════════════════════════════════════════════════════════
# The write version string derived from the manifest crawl_id
# ═══════════════════════════════════════════════════════════════════════


class TestExtractorVersionFromCrawlId:
    def test_version_string_is_the_qec_live_v1_form(self):
        bundle = _map(_load_records())
        version = _extractor_version(bundle.crawl_id)
        assert version == f"{_EXTRACTOR_VERSION_PREFIX}{bundle.crawl_id}"
        assert version == "qec_live_v1@expl-3page-manifest-01"
        assert len(version) <= 50  # page_visits.extractor_version String(50) cap


# ═══════════════════════════════════════════════════════════════════════
# Cross-subsystem field alignment: emit.py dataclasses ⊇ schema fields
# (subprocess — the explorer package owns its own top-level `app` package)
# ═══════════════════════════════════════════════════════════════════════

_ALIGN_SCRIPT = r"""
import dataclasses, json, os, sys
sys.path.insert(0, os.environ["QEC_EXPLORER_ROOT"])
try:
    from app.emit import PageStateRecord, ActionRecord, ScreenshotRecord
except Exception as exc:
    print(f"IMPORT_ERROR: {exc}", file=sys.stderr)
    sys.exit(3)

def names(dc):
    return sorted(f.name for f in dataclasses.fields(dc))

print(json.dumps({
    "page_state": names(PageStateRecord),
    "action": names(ActionRecord),
    "screenshot": names(ScreenshotRecord),
}))
"""


class TestEmitRecordsAlignWithSchema:
    def test_emit_dataclasses_cover_the_mapper_fields(self):
        proc = subprocess.run(
            [sys.executable, "-c", _ALIGN_SCRIPT],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "QEC_EXPLORER_ROOT": str(EXPLORER_ROOT)},
        )
        if proc.returncode == 3:
            pytest.skip(f"explorer emit.py not importable here: {proc.stderr.strip()[:200]}")
        assert proc.returncode == 0, f"align subprocess failed:\n{proc.stderr[-2000:]}"
        fields = json.loads(proc.stdout.strip().splitlines()[-1])

        page_fields = set(fields["page_state"])
        assert set(PAGE_STATE_SCHEMA_FIELDS) <= page_fields
        assert {"actions", "screenshots"} <= page_fields
        assert set(MANIFEST_ONLY_PAGE_FIELDS) <= page_fields

        action_fields = set(fields["action"])
        assert set(ACTION_SCHEMA_FIELDS) <= action_fields
        assert set(MANIFEST_ONLY_ACTION_FIELDS) <= action_fields

        # ScreenshotRecord replaces png_base64 with a staged-file `path` (R-5)
        assert set(fields["screenshot"]) == {"frame_index", "timestamp_ms", "path"}
