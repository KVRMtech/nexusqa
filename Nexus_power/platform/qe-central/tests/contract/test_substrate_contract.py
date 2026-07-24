"""QE-Central — THE golden substrate contract test (design §2.4, pinned in CI).

Implements §2.4 items 1-6:

  1. Golden fixture (``golden_3page_form.json``) → ``substrate/writer.py`` →
     the REAL ``factory_service._load_current_pages_and_actions`` against the
     same DB; visits / actions / form_snapshot / anchor / after round-trip
     EXACTLY.                                             [DB]
  2. Constraint names ``uq_page_visits_artifact_sequence_version`` and
     ``uq_page_actions_visit_version_subaction`` exist; ``tenant_isolation``
     policy + ``relforcerowsecurity`` on page_visits / page_actions /
     ground_truth_events.                                 [DB]
  3. Two-version write with staggered created_at → the loader returns ONLY
     v-new (pins service.py:44-58 latest-wins).           [DB]
  4. Navigation-proof: generated cases from the fixture carry the hard
     toHaveURL signal (source='ground_truth' + confidence 1.0 ≥ 0.9 —
     pins generator.py:556, 581-585).                     [DB]
  5. Honesty pins: ``judge_semantic_match`` with a missing baseline →
     uncertain sentinel (semantic_oracle.py:148-167)      [pure, subprocess];
     ``GET /playwright`` with zero cases → honest 404 and viewer-role JWT →
     403 on /generate (test_factory.py:541-548 / :114-125) [live stack].
  6. Every emitted ``frame_asset_path`` satisfies
     ``safe_frame_asset_path(tenant, path) == path`` and ``_frame_refs`` maps
     each visit to its screenshot.                        [pure + DB]

DB-requiring items are gated on ``QEC_TEST_DATABASE_URL`` (a DISPOSABLE
nexus-shaped Postgres at alembic head — substrate tables + RLS present) so
the suite stays green locally without Postgres.  Pure items — golden-fixture
→ ``ExplorationBundle`` schema round-trip, redaction behaviour, the
ONE-version-string rule, the ``frame_asset_path`` guard mirror, and the
``qec_live_v1@{crawl_id}`` version format — ALWAYS run.

The factory loader/generator are exercised in a SUBPROCESS with
``platform/api`` on ``sys.path``: both services own a top-level package named
``app``, and QE-Central's bounded-context rule (R-1) forbids importing
VKPower code in-process — the shared seam is the DATABASE.
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from app.config import settings
from app.db import safe_frame_asset_path
from app.substrate import assets as substrate_assets
from app.substrate.assets import frame_asset_rel_path, frame_file_name
from app.substrate.redact import (
    PII_CLASSES,
    placeholder,
    redact_secret,
    redact_value,
)
from app.substrate.schema import (
    ACTION_VERBS,
    TARGET_KINDS,
    ExplorationBundle,
    RefusalError,
    validate_bundle,
)
from app.substrate.writer import (
    EXTRACTOR_VERSION_MAX,
    EXTRACTOR_VERSION_PREFIX,
    VISIT_SOURCE,
    redacted_action_value,
    redacted_form_snapshot,
    validate_extractor_version,
)

_TESTS_DIR = Path(__file__).resolve().parent
SERVICE_ROOT = _TESTS_DIR.parent.parent                      # platform/qe-central
PLATFORM_API_ROOT = SERVICE_ROOT.parent / "api"              # platform/api
SDK_ROOT = SERVICE_ROOT.parent.parent / "sdk" / "nexus-sdk"  # sdk/nexus-sdk
FIXTURE_PATH = SERVICE_ROOT / "app" / "harness" / "fixtures" / "golden_3page_form.json"

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
API_URL = os.environ.get("QEC_TEST_PLATFORM_API_URL", "")

needs_db = pytest.mark.skipif(
    not DB_URL,
    reason=(
        "QEC_TEST_DATABASE_URL not set — §2.4 DB items need a disposable "
        "nexus-shaped Postgres at alembic head"
    ),
)
needs_live_api = pytest.mark.skipif(
    not (DB_URL and API_URL),
    reason=(
        "QEC_TEST_PLATFORM_API_URL + QEC_TEST_DATABASE_URL not set — §2.4 "
        "item-5 HTTP honesty pins need the live factory sharing the same DB "
        "and NEXUS_JWT_SECRET"
    ),
)


def _load_fixture() -> dict:
    with FIXTURE_PATH.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _fresh_bundle_dict(tag: str) -> dict:
    """Golden fixture with a fresh crawl_id (fresh immutable version)."""
    bundle = _load_fixture()
    bundle["crawl_id"] = f"ct{uuid.uuid4().hex[:16]}-{tag}"[:36]
    return bundle


def _fixture_actions(bundle_dict: dict) -> list[dict]:
    return [a for p in bundle_dict["pages"] for a in p.get("actions") or []]


def _fixture_screenshots(bundle_dict: dict) -> list[dict]:
    return [s for p in bundle_dict["pages"] for s in p.get("screenshots") or []]


# ═══════════════════════════════════════════════════════════════════════
# PURE — golden fixture ⇄ ExplorationBundle schema round-trip (§2.4 item 1)
# ═══════════════════════════════════════════════════════════════════════


class TestGoldenFixtureSchemaRoundTrip:
    def test_fixture_shape_is_the_design_golden(self):
        """3 visits login→form→confirm, 9 actions w/ anchor+after, 1 PII
        (password-signalled) field, 3 screenshots (§2.4 item 1)."""
        bundle = _load_fixture()
        assert len(bundle["pages"]) == 3
        assert [p["url_path"] for p in bundle["pages"]] == ["/login", "/apply", "/confirm"]
        assert len(_fixture_actions(bundle)) == 9
        assert len(_fixture_screenshots(bundle)) == 3
        assert bundle["frame_count"] == 3
        password_signals = [
            label
            for p in bundle["pages"]
            for label, sig in (p.get("form_snapshot_signals") or {}).items()
            if sig.get("type") == "password"
        ]
        assert password_signals == ["Password"], "exactly one PII field"

    def test_fixture_validates_and_round_trips_exactly(self):
        raw = _load_fixture()
        bundle = ExplorationBundle.model_validate(raw)
        # dump → re-validate → dump must be byte-stable (schema round-trip)
        first = bundle.model_dump()
        second = ExplorationBundle.model_validate(first).model_dump()
        assert first == second
        # defence-in-depth re-validation returns an equal bundle
        assert validate_bundle(bundle).model_dump() == first
        assert bundle.total_actions() == 9
        assert sum(len(p.screenshots) for p in bundle.pages) == bundle.frame_count == 3

    def test_fixture_vocabulary_pinned_to_migration_035(self):
        """Verb / target_kind values are the REAL substrate column vocab
        (alembic 035_page_actions.py:61-65) — anything else the factory
        cannot bind on."""
        bundle = ExplorationBundle.model_validate(_load_fixture())
        for page in bundle.pages:
            for action in page.actions:
                assert action.verb in ACTION_VERBS
                assert action.target_kind in TARGET_KINDS
                assert action.after is not None, "after bundle is mandatory evidence"
                assert action.anchor is not None, "golden actions are fully grounded"

    def test_every_grounded_navigation_is_recorded_not_inferred(self):
        bundle = ExplorationBundle.model_validate(_load_fixture())
        nav_actions = [
            a for p in bundle.pages for a in p.actions
            if a.after and a.after.outcome == "navigation"
        ]
        assert len(nav_actions) == 3
        for a in nav_actions:
            assert a.after.navigated is True
            assert a.url_changed is True

    def test_broken_evidence_refuses_with_enumerated_reason(self):
        raw = _load_fixture()
        raw["pages"][2]["sequence_index"] = raw["pages"][0]["sequence_index"]
        with pytest.raises(Exception) as exc_info:
            validate_bundle(ExplorationBundle.model_construct(**raw))
        # a duplicated sequence_index must surface the honest enumerated reason
        assert "non_monotonic_sequence_index" in str(exc_info.value) or isinstance(
            exc_info.value, RefusalError
        )

    def test_screenshot_timestamps_inside_visit_windows(self):
        """The factory's _frame_refs window-join depends on this
        (service.py:61-89) — the schema must refuse a shot outside its
        owning visit's [first_seen_ms, last_seen_ms]."""
        raw = _load_fixture()
        raw["pages"][0]["screenshots"][0]["timestamp_ms"] = 999999
        with pytest.raises(Exception) as exc_info:
            ExplorationBundle.model_validate(raw)
        assert "screenshot_outside_visit_window" in str(exc_info.value)


# ═══════════════════════════════════════════════════════════════════════
# PURE — redaction at source (§2.1 / R6)
# ═══════════════════════════════════════════════════════════════════════


class TestRedactionAtSource:
    def test_regex_classes_are_replaced_with_uniform_placeholders(self):
        result = redact_value("SSN 078-05-1120 card 4111 1111 1111 1111 mail a@b.co")
        assert "078-05-1120" not in result.value
        assert "4111 1111 1111 1111" not in result.value
        assert "a@b.co" not in result.value
        assert placeholder("ssn") in result.value
        assert placeholder("card") in result.value
        assert placeholder("email") in result.value
        assert result.errors == 0
        assert result.total == sum(result.counts.values()) >= 3

    def test_password_secret_redacted_whole_via_signals(self):
        result = redact_secret("hunter2-super-secret!")
        assert result.value == "[REDACTED:password]"
        assert result.counts == {"password": 1}
        # empty committed value (the explorer never captures raw passwords)
        assert redact_secret("").value == ""
        assert redact_secret(None).value == ""

    def test_placeholder_classes_are_the_pinned_set(self):
        assert set(PII_CLASSES) == {"ssn", "card", "password", "email", "phone", "dob"}
        for cls in PII_CLASSES:
            assert placeholder(cls) == f"[REDACTED:{cls}]"

    def test_writer_redacts_password_signalled_snapshot_fields_whole(self):
        bundle = ExplorationBundle.model_validate(_load_fixture())
        login = bundle.pages[0]
        # give the password field a value to prove whole-value redaction
        login.form_snapshot["Password"] = "raw-password-must-never-persist"
        snapshot, counts, errors = redacted_form_snapshot(login)
        assert snapshot["Password"] == "[REDACTED:password]"
        assert snapshot["Username"] == "casey.tester"
        assert counts.get("password") == 1
        assert errors == 0

    def test_writer_redacts_password_flagged_action_values_whole(self):
        bundle = ExplorationBundle.model_validate(_load_fixture())
        login = bundle.pages[0]
        pw_action = copy.deepcopy(login.actions[1])  # 'type Password'
        object.__setattr__(pw_action, "value", "raw-password-must-never-persist")
        result = redacted_action_value(pw_action, login)
        assert result.value == "[REDACTED:password]"

    def test_detector_failure_is_fail_open_and_counted_never_silent(self, monkeypatch):
        import nexus_sdk.evidence.pii_detector as pii_detector

        def _boom(_text, *a, **kw):
            raise RuntimeError("detector unavailable")

        monkeypatch.setattr(pii_detector, "detect_pii", _boom)
        result = redact_value("SSN 078-05-1120")
        assert result.value == "SSN 078-05-1120"  # passes through unredacted
        assert result.errors == 1                 # ...but VISIBLY counted
        assert result.counts == {}


# ═══════════════════════════════════════════════════════════════════════
# PURE — the ONE-version-string rule + qec_live_v1@{crawl_id} format (§2.3)
# ═══════════════════════════════════════════════════════════════════════


class TestOneVersionStringRule:
    def test_format_is_qec_live_v1_at_crawl_id(self):
        crawl_id = _load_fixture()["crawl_id"]
        version = f"{EXTRACTOR_VERSION_PREFIX}{crawl_id}"
        assert EXTRACTOR_VERSION_PREFIX == "qec_live_v1@"
        assert validate_extractor_version(version) == version
        assert version == f"qec_live_v1@{crawl_id}"
        # a full uuid4 crawl_id still fits the String(50) column
        assert len(f"{EXTRACTOR_VERSION_PREFIX}{uuid.uuid4().hex}") <= EXTRACTOR_VERSION_MAX

    def test_every_writer_entry_point_mints_the_same_string(self):
        """Router, harness runner and writer must agree byte-for-byte —
        a divergent prefix would corrupt latest-wins selection."""
        from app.harness import runner as harness_runner
        from app.routers import explorations as explorations_router

        crawl_id = "abc123-crawl"
        assert (
            explorations_router._extractor_version(crawl_id)
            == harness_runner._extractor_version(crawl_id)
            == f"{EXTRACTOR_VERSION_PREFIX}{crawl_id}"
        )
        assert (
            explorations_router._EXTRACTOR_VERSION_PREFIX
            == harness_runner._EXTRACTOR_VERSION_PREFIX
            == EXTRACTOR_VERSION_PREFIX
        )

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "v1", "qec_live_v2@x", "pages_forms_v3",
         "qec_live_v1@" + "x" * 60],
    )
    def test_malformed_versions_refuse_honestly(self, bad):
        with pytest.raises(RefusalError) as exc_info:
            validate_extractor_version(bad)
        assert exc_info.value.reason == "invalid_extractor_version"

    def test_visit_source_is_the_proven_gate_literal(self):
        """source='ground_truth' is the EXISTING generator PROVEN-gate value
        (generator.py:556 _PROVEN_SOURCES) — deliberate, §2.2."""
        assert VISIT_SOURCE == "ground_truth"


# ═══════════════════════════════════════════════════════════════════════
# PURE — frame_asset_path guard mirror (§2.4 item 6, pure half)
# ═══════════════════════════════════════════════════════════════════════


class TestFrameAssetPathMirror:
    def test_emitted_shape_passes_the_serving_guard(self):
        tenant, session, crawl = "tenant-a", "sess-1", "crawl99"
        for idx in (0, 4, 12345):
            path = frame_asset_rel_path(tenant, session, crawl, idx)
            assert path == f"{tenant}/{session}/{crawl}_frames/{frame_file_name(idx)}"
            assert safe_frame_asset_path(tenant, path) == path

    def test_guard_rejects_traversal_cross_tenant_and_non_images(self):
        assert safe_frame_asset_path("t-a", "t-a/../x/frame_00001.png") == ""
        assert safe_frame_asset_path("t-a", "t-b/s/c_frames/frame_00001.png") == ""
        assert safe_frame_asset_path("t-a", "t-a\\s\\c_frames\\f.png") == ""
        assert safe_frame_asset_path("t-a", "t-a/s/c_frames/frame_00001.exe") == ""
        assert safe_frame_asset_path("t-a", "") == ""
        assert safe_frame_asset_path("t-a", None) == ""

    def test_golden_fixture_frames_would_be_servable(self):
        bundle = ExplorationBundle.model_validate(_load_fixture())
        tenant, session = "tenant-golden", "sess-golden"
        for page in bundle.pages:
            for shot in page.screenshots:
                path = frame_asset_rel_path(tenant, session, bundle.crawl_id, shot.frame_index)
                assert safe_frame_asset_path(tenant, path) == path

    def test_negative_frame_index_refused(self):
        with pytest.raises(ValueError):
            frame_file_name(-1)


# ═══════════════════════════════════════════════════════════════════════
# Item 5 (pure half, subprocess) — semantic-oracle uncertain sentinel
# ═══════════════════════════════════════════════════════════════════════

_JUDGE_SCRIPT = r"""
import asyncio, json, os, sys
sys.path.insert(0, os.environ["QEC_PLATFORM_API_ROOT"])
sys.path.insert(0, os.environ["QEC_SDK_ROOT"])
try:
    from app.services.test_factory.semantic_oracle import judge_semantic_match
except Exception as exc:
    print(f"IMPORT_ERROR: {exc}", file=sys.stderr)
    sys.exit(3)

async def main():
    missing_baseline = await judge_semantic_match("expected", None, b"actual", object())
    missing_actual = await judge_semantic_match("expected", b"base", None, object())
    missing_router = await judge_semantic_match("expected", b"base", b"actual", None)
    print(json.dumps({
        "missing_baseline": missing_baseline,
        "missing_actual": missing_actual,
        "missing_router": missing_router,
    }))

asyncio.run(main())
"""


def _subprocess_env() -> dict:
    env = dict(os.environ)
    env["QEC_PLATFORM_API_ROOT"] = str(PLATFORM_API_ROOT)
    env["QEC_SDK_ROOT"] = str(SDK_ROOT)
    if DB_URL:
        env["QEC_TEST_DATABASE_URL"] = DB_URL
    return env


class TestSemanticOracleHonestyPin:
    def test_missing_baseline_returns_uncertain_sentinel_never_match(self):
        """Pins semantic_oracle.py:148-167 — every no-op path returns the
        uncertain sentinel.  Runs in a subprocess: platform/api and
        qe-central both own a top-level ``app`` package (R-1 forbids
        importing VKPower in-process)."""
        proc = subprocess.run(
            [sys.executable, "-c", _JUDGE_SCRIPT],
            capture_output=True, text=True, env=_subprocess_env(), timeout=180,
        )
        if proc.returncode == 3:
            pytest.skip(f"platform/api factory not importable here: {proc.stderr.strip()[:300]}")
        assert proc.returncode == 0, f"judge subprocess failed:\n{proc.stderr[-2000:]}"
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        for key, judgement in out.items():
            assert judgement["semantic_match"] == "uncertain", key
            assert judgement["semantic_match"] != "match"


# ═══════════════════════════════════════════════════════════════════════
# DB-REQUIRING ITEMS (§2.4 items 1-4, 6 — disposable Postgres at head)
# ═══════════════════════════════════════════════════════════════════════

_LOADER_SCRIPT = r"""
import asyncio, json, os, sys
from dataclasses import asdict
sys.path.insert(0, os.environ["QEC_PLATFORM_API_ROOT"])
sys.path.insert(0, os.environ["QEC_SDK_ROOT"])
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool  # test-only: no cross-event-loop pooling
from app.services.test_factory.service import (
    _latest_version,
    _load_current_pages_and_actions,
)
from app.services.test_factory.generator import generate_demonstrated_test_cases
from nexus_sdk.db.models import PageVisitRow

async def main():
    tenant_id, artifact_id = sys.argv[1], sys.argv[2]
    engine = create_async_engine(os.environ["QEC_TEST_DATABASE_URL"], poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            await session.execute(
                text("SELECT set_config('nexus.current_tenant_id', :tid, true)"),
                {"tid": tenant_id},
            )
            visits, actions = await _load_current_pages_and_actions(
                session, artifact_id=artifact_id,
            )
            current_version = await _latest_version(session, PageVisitRow, artifact_id)
        result = generate_demonstrated_test_cases(
            artifact_id=artifact_id, page_visits=visits, page_actions=actions,
        )
        print(json.dumps({
            "current_version": current_version or "",
            "visits": [asdict(v) for v in visits],
            "actions": [asdict(a) for a in actions],
            "cases": [c.model_dump() for c in result.test_cases],
        }, default=str))
    finally:
        await engine.dispose()

asyncio.run(main())
"""


def _run_factory_subprocess(tenant_id: str, artifact_id: str) -> dict:
    """Drive the REAL factory loader + generator in-process-with-the-DB
    (design §2.4 item 1 'call factory_service._load_current_pages_and_actions
    in-process against the same DB')."""
    proc = subprocess.run(
        [sys.executable, "-c", _LOADER_SCRIPT, tenant_id, artifact_id],
        capture_output=True, text=True, env=_subprocess_env(), timeout=300,
    )
    assert proc.returncode == 0, f"factory loader subprocess failed:\n{proc.stderr[-3000:]}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


@asynccontextmanager
async def _patched_substrate(tmp_path):
    """Point the qe-central writer stack at the disposable test DB + a tmp
    local ArtifactStore, restoring everything afterwards."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from nexus_sdk.storage import create_storage
    from nexus_sdk.storage.artifact_store import ArtifactStore
    from nexus_sdk.storage.base import StorageConfig

    import app.db as qec_db

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cfg = StorageConfig(backend="local", local_root=str(tmp_path / "artifact-store"))

    orig_factory = qec_db._substrate_session_factory
    orig_store = substrate_assets._store_cache
    orig_frames_path = settings.nexus_frame_storage_path
    qec_db._substrate_session_factory = factory
    substrate_assets._store_cache = ArtifactStore(create_storage(cfg), cfg)
    settings.nexus_frame_storage_path = str(tmp_path / "frames")
    try:
        yield engine
    finally:
        qec_db._substrate_session_factory = orig_factory
        substrate_assets._store_cache = orig_store
        settings.nexus_frame_storage_path = orig_frames_path
        await engine.dispose()


async def _create_and_write(bundle_dict: dict, tenant_id: str):
    """creator → writer, exactly the explorations-router sequence."""
    from app.artifacts.creator import create_crawl_artifact
    from app.substrate.writer import write_exploration

    bundle = ExplorationBundle.model_validate(bundle_dict)
    created = await create_crawl_artifact(
        tenant_id=tenant_id,
        target_url=bundle.target_url,
        crawl_id=bundle.crawl_id,
        config_fingerprint=bundle.config_fingerprint,
        frame_count=bundle.frame_count,
        meta={"contract_test": "true"},
    )
    version = f"{EXTRACTOR_VERSION_PREFIX}{bundle.crawl_id}"
    stats = await write_exploration(
        bundle,
        tenant_id=tenant_id,
        artifact_id=created.artifact_id,
        session_id=created.session_id,
        extractor_version=version,
    )
    return created, version, stats


def _fresh_tenant() -> str:
    return f"qec-contract-{uuid.uuid4().hex[:10]}"


@needs_db
class TestGoldenRoundTripThroughTheRealLoader:
    """§2.4 item 1 (+ item 6 DB half)."""

    def test_write_then_loader_round_trips_exactly(self, tmp_path):
        asyncio.run(self._run(tmp_path))

    async def _run(self, tmp_path):
        from sqlalchemy import text

        tenant_id = _fresh_tenant()
        bundle_dict = _fresh_bundle_dict("rt")

        async with _patched_substrate(tmp_path) as engine:
            created, version, stats = await _create_and_write(bundle_dict, tenant_id)
            assert stats.visits == 3
            assert stats.actions == 9
            assert stats.screenshots == 3
            assert stats.frames == 3
            assert stats.events == 12  # 3 visit navigations + 9 actions
            assert stats.unredacted_errors == 0

            # item 6 (DB): every persisted frame_asset_path is servable
            async with engine.connect() as conn:
                rows = (await conn.execute(text(
                    "SELECT frame_asset_path FROM visual_frames "
                    "WHERE artifact_id = :a ORDER BY frame_index"
                ), {"a": created.artifact_id})).scalars().all()
            assert len(rows) == 3
            for idx, path in enumerate(rows):
                assert safe_frame_asset_path(tenant_id, path) == path
                assert path == frame_asset_rel_path(
                    tenant_id, created.session_id, bundle_dict["crawl_id"], idx,
                )

        out = _run_factory_subprocess(tenant_id, created.artifact_id)
        assert out["current_version"] == version

        # ── visits round-trip EXACTLY ────────────────────────────────
        visits = sorted(out["visits"], key=lambda v: v["sequence_index"])
        pages = bundle_dict["pages"]
        assert [v["sequence_index"] for v in visits] == [p["sequence_index"] for p in pages]
        for visit, page in zip(visits, pages):
            assert visit["location"] == page["location"]
            assert visit["url_host"] == page["url_host"]
            assert visit["url_path"] == page["url_path"]
            assert visit["url_query"] == page["url_query"]
            assert visit["canonical_host"] == page["canonical_host"]
            assert visit["source"] == "ground_truth"
            assert float(visit["extraction_confidence"]) == 1.0
            assert visit["form_snapshot"] == page["form_snapshot"]
            assert visit["form_snapshot_signals"] == page["form_snapshot_signals"]
            assert int(visit["first_seen_ms"]) == page["first_seen_ms"]
            assert int(visit["duration_ms"]) == page["last_seen_ms"] - page["first_seen_ms"]
            # item 6: _frame_refs maps each visit to ITS OWN screenshot
            expected_frame = frame_asset_rel_path(
                tenant_id, created.session_id, bundle_dict["crawl_id"],
                page["screenshots"][0]["frame_index"],
            )
            assert visit["frame_ref"] == expected_frame

        # ── actions round-trip EXACTLY (anchor / after / value) ─────
        visit_id_by_seq = {v["sequence_index"]: v["page_visit_id"] for v in visits}
        loader_actions = {
            (a["page_visit_id"], a["subaction_index"]): a for a in out["actions"]
        }
        assert len(loader_actions) == 9
        for page in pages:
            for action in page["actions"]:
                key = (visit_id_by_seq[page["sequence_index"]], action["subaction_index"])
                got = loader_actions[key]
                assert got["verb"] == action["verb"]
                assert got["target_label"] == action["target_label"]
                assert got["target_kind"] == action["target_kind"]
                assert got["value"] == action.get("value")
                assert got["anchor"] == action["anchor"]["label"]
                assert got["anchor_kind"] == action["anchor"]["kind"]
                assert got["after_outcome"] == action["after"]["outcome"]
                assert got["after_detail"] == action["after"]["detail"]
                expected_navigated = (
                    action["after"]["outcome"] == "navigation"
                    or bool(action.get("url_changed"))
                    or bool(action["after"].get("navigated"))
                )
                assert bool(got["navigated"]) is expected_navigated

    def test_duplicate_version_write_is_refused(self, tmp_path):
        """§2.3 rule 2: one write = one immutable version."""
        asyncio.run(self._run_duplicate(tmp_path))

    async def _run_duplicate(self, tmp_path):
        from app.substrate.writer import write_exploration

        tenant_id = _fresh_tenant()
        bundle_dict = _fresh_bundle_dict("dup")
        async with _patched_substrate(tmp_path):
            created, version, _ = await _create_and_write(bundle_dict, tenant_id)
            with pytest.raises(RefusalError) as exc_info:
                await write_exploration(
                    ExplorationBundle.model_validate(bundle_dict),
                    tenant_id=tenant_id,
                    artifact_id=created.artifact_id,
                    session_id=created.session_id,
                    extractor_version=version,
                )
            assert exc_info.value.reason == "duplicate_extractor_version"


@needs_db
class TestConstraintAndRlsNames:
    """§2.4 item 2 — the seams a VKPower refactor would move."""

    def test_unique_constraints_policies_and_force_rls(self):
        asyncio.run(self._run())

    async def _run(self):
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        engine = create_async_engine(DB_URL, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                constraints = set((await conn.execute(text(
                    "SELECT conname FROM pg_constraint WHERE conname IN "
                    "('uq_page_visits_artifact_sequence_version',"
                    " 'uq_page_actions_visit_version_subaction')"
                ))).scalars().all())
                policies = {
                    (r[0], r[1]) for r in (await conn.execute(text(
                        "SELECT c.relname, p.polname FROM pg_policy p "
                        "JOIN pg_class c ON c.oid = p.polrelid "
                        "WHERE c.relname IN "
                        "('page_visits','page_actions','ground_truth_events')"
                    ))).all()
                }
                rls = {
                    r[0]: (r[1], r[2]) for r in (await conn.execute(text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity "
                        "FROM pg_class WHERE relname IN "
                        "('page_visits','page_actions','ground_truth_events')"
                    ))).all()
                }
        finally:
            await engine.dispose()

        assert constraints == {
            "uq_page_visits_artifact_sequence_version",
            "uq_page_actions_visit_version_subaction",
        }
        for table in ("page_visits", "page_actions", "ground_truth_events"):
            assert (table, "tenant_isolation") in policies, f"{table} missing policy"
            enabled, forced = rls[table]
            assert enabled is True, f"{table} RLS not enabled"
            assert forced is True, f"{table} RLS not FORCEd"


@needs_db
class TestTwoVersionLatestWins:
    """§2.4 item 3 — pins service.py:44-58."""

    def test_loader_returns_only_v_new(self, tmp_path):
        asyncio.run(self._run(tmp_path))

    async def _run(self, tmp_path):
        from sqlalchemy import text

        tenant_id = _fresh_tenant()
        old_dict = _fresh_bundle_dict("old")
        old_dict["pages"][1]["form_snapshot"]["Full name"] = "Casey OLD-VERSION"

        new_dict = copy.deepcopy(old_dict)
        new_dict["crawl_id"] = f"ct{uuid.uuid4().hex[:16]}-new"[:36]
        new_dict["pages"][1]["form_snapshot"]["Full name"] = "Casey NEW-VERSION"

        async with _patched_substrate(tmp_path) as engine:
            created_old, old_version, _ = await _create_and_write(old_dict, tenant_id)
            await asyncio.sleep(0.2)  # staggered created_at (latest-wins key)
            created_new, new_version, _ = await _create_and_write(new_dict, tenant_id)

            # identical (url, config, explorer) fingerprint → SAME artifact
            assert created_new.artifact_id == created_old.artifact_id
            assert created_new.reused is True

            async with engine.connect() as conn:
                per_version = dict((await conn.execute(text(
                    "SELECT extractor_version, COUNT(*) FROM page_visits "
                    "WHERE artifact_id = :a GROUP BY extractor_version"
                ), {"a": created_old.artifact_id})).all())
            # stale versions remain immutable history
            assert per_version == {old_version: 3, new_version: 3}

        out = _run_factory_subprocess(tenant_id, created_old.artifact_id)
        assert out["current_version"] == new_version
        assert len(out["visits"]) == 3, "loader must return ONLY the current version"
        apply_visit = next(v for v in out["visits"] if v["url_path"] == "/apply")
        assert apply_visit["form_snapshot"]["Full name"] == "Casey NEW-VERSION"
        assert len(out["actions"]) == 9


@needs_db
class TestNavigationProof:
    """§2.4 item 4 — pins generator.py:556, 581-585."""

    def test_generated_cases_carry_hard_navigation_assertions(self, tmp_path):
        asyncio.run(self._run(tmp_path))

    async def _run(self, tmp_path):
        tenant_id = _fresh_tenant()
        bundle_dict = _fresh_bundle_dict("nav")
        async with _patched_substrate(tmp_path):
            created, _, _ = await _create_and_write(bundle_dict, tenant_id)

        out = _run_factory_subprocess(tenant_id, created.artifact_id)

        # the PROVEN-gate inputs: source='ground_truth', confidence 1.0 ≥ 0.9
        for visit in out["visits"]:
            assert visit["source"] == "ground_truth"
            assert float(visit["extraction_confidence"]) >= 0.9

        cases = out["cases"]
        assert cases, "golden fixture must generate at least one case"
        demonstrated_navs = [
            step
            for case in cases
            for step in case.get("steps", [])
            if (step.get("observed") or {}).get("verb") == "navigate"
            and step.get("provenance") == "demonstrated"
        ]
        assert demonstrated_navs, (
            "no navigation step earned 'demonstrated' provenance — the hard "
            "toHaveURL gate (generator.py:556,581-585) did not fire"
        )
        for step in demonstrated_navs:
            assert str(step.get("selector") or "").startswith("url="), (
                "a demonstrated navigation must carry the url selector the "
                "compiler turns into a hard toHaveURL"
            )


# ═══════════════════════════════════════════════════════════════════════
# Item 5 (live-stack half) — HTTP honesty pins
# ═══════════════════════════════════════════════════════════════════════


@needs_live_api
class TestFactoryHttpHonestyPins:
    """§2.4 item 5 — /playwright zero-case 404 + viewer-role 403.

    Needs the LIVE platform-api sharing this test DB and NEXUS_JWT_SECRET
    (the REFUSE harness R1/R5 rules also pin these against the deployed
    stack — this is the CI-local mirror)."""

    def test_playwright_with_zero_cases_is_honest_404(self, tmp_path):
        asyncio.run(self._run_404(tmp_path))

    async def _run_404(self, tmp_path):
        import httpx

        from app.artifacts.creator import create_crawl_artifact
        from app.service_token import mint_service_jwt

        tenant_id = _fresh_tenant()
        async with _patched_substrate(tmp_path):
            created = await create_crawl_artifact(
                tenant_id=tenant_id,
                target_url="https://portal.golden-fixture.test/login",
                crawl_id=f"ct{uuid.uuid4().hex[:16]}-404"[:36],
                config_fingerprint=f"cf-{uuid.uuid4().hex}",
                frame_count=0,
                meta={"contract_test": "true"},
            )
        token = mint_service_jwt(tenant_id)
        async with httpx.AsyncClient(base_url=API_URL, timeout=60.0) as client:
            response = await client.get(
                f"/api/v1/test-factory/{created.artifact_id}/playwright",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 404, (
            f"zero-case /playwright must be an honest 404, got "
            f"{response.status_code}: {response.text[:300]}"
        )

    def test_viewer_role_is_403_on_generate(self):
        asyncio.run(self._run_403())

    async def _run_403(self):
        import httpx
        import jwt as pyjwt
        from datetime import datetime, timedelta, timezone

        tenant_id = _fresh_tenant()
        viewer_token = pyjwt.encode(
            {
                "sub": "qec-contract-viewer",
                "email": "viewer@contract.test",
                "role": "viewer",
                "tenant_id": tenant_id,
                "exp": datetime.now(timezone.utc) + timedelta(minutes=10),
            },
            settings.nexus_jwt_secret,
            algorithm="HS256",
        )
        async with httpx.AsyncClient(base_url=API_URL, timeout=60.0) as client:
            response = await client.post(
                f"/api/v1/test-factory/{uuid.uuid4()}/generate",
                json={},
                headers={"Authorization": f"Bearer {viewer_token}"},
            )
        assert response.status_code == 403, (
            f"viewer role must be 403 on /generate (RBAC, test_factory.py:"
            f"114-125), got {response.status_code}"
        )
