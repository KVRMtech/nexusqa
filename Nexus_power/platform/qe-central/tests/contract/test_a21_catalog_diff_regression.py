"""GATE 3 / A21 — THE CONSUMER HALF: two real crawls, three deliberate changes,
and a diff that names each one correctly.

    real coverage (baseline)          →  fold_crawl  →  catalog version 1
    real coverage (after 3 changes)   →  fold_crawl  →  catalog version 2
    →  diff  →  added / removed / changed, each matching the change that caused it

WHAT IS REAL HERE, STATED SO THE CLAIM CANNOT BE OVER-READ
==========================================================
The two coverage accounts are the byte-for-byte output of two real Chromium
crawls of ``proving-grounds/acme-life`` through the production ``Crawler`` and
``PlaywrightBrowserPort``, recorded by
``engines/qe-explorer/tests/browser/test_a21_catalog_diff_regression.py``. They
are not fixtures, and this module deliberately has no fallback to one: A21's
whole claim is that REAL crawl evidence classifies correctly, and a hand-written
snapshot cannot make that claim.

Everything on this side is production code — ``fold_crawl``,
``build_app_master_catalog``, ``persist_catalog_version``,
``diff_latest_versions``, ``diff_catalogs`` — against a real Postgres. The two
services cannot share a process (M1.7 froze that boundary as data), so the
recorded accounts ARE the seam between them.

WHY ALL THREE CLASSIFICATIONS IN ONE DIFF AND NOT THREE RUNS
============================================================
Because a diff that reports one change correctly in isolation is a weaker claim
than one that separates three simultaneous changes. Run separately, "removed"
could be produced by any catalogue that shrank; run together, the diff has to put
each question in the right bucket while the other two are moving, and the
``unchanged`` set has to hold everything else still.

THE ``changed`` CLASSIFICATION IS THE ONE THAT TESTS IDENTITY
=============================================================
``added`` and ``removed`` are set differences over ``question_id``. ``changed``
requires the SAME id on both sides with a diffed field moving underneath it —
which only happens if the catalogue's identity model is stable across the change.
The producer's docstring records the measurement that makes this possible: the
answer set grows from 3 options to 4, and ``field_signature._option_shape``
buckets both as ``few``, so the signature — and therefore the ``question_id`` —
is unchanged. A bigger change would cross into the ``many`` bucket and be
reported as a removal plus an addition instead.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the A21 catalog-diff regression "
           "folds two real crawls into a disposable Postgres",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "proving-grounds").is_dir() and (parent / "engines").is_dir():
            return parent
    raise AssertionError(f"Nexus_power root not found above {here}")


ROOT = _repo_root()
EVIDENCE = ROOT / "evidence" / "a21_catalog_diff"
ACME_INDEX = ROOT / "proving-grounds" / "acme-life" / "index.html"

_FILES = ("stamp.json", "coverage_baseline.json", "coverage_after_change.json")


def _evidence() -> dict[str, Any]:
    """The two real coverage accounts plus the producer's stamp."""
    missing = [n for n in _FILES if not (EVIDENCE / n).is_file()]
    assert not missing, (
        f"the A21 crawl evidence is missing {missing} from {EVIDENCE}.\n"
        f"Regenerate it with the producer half:\n"
        f"  cd engines/qe-explorer && python -m pytest "
        f"tests/browser/test_a21_catalog_diff_regression.py\n"
        f"This module deliberately does NOT fall back to a hand-written fixture: "
        f"A21's whole claim is that a real application change is classified "
        f"correctly from real crawl evidence, and a fixture cannot make it.")
    return {
        "stamp": json.loads((EVIDENCE / "stamp.json").read_text(encoding="utf-8")),
        "before": json.loads(
            (EVIDENCE / "coverage_baseline.json").read_text(encoding="utf-8")),
        "after": json.loads(
            (EVIDENCE / "coverage_after_change.json").read_text(encoding="utf-8")),
    }


@asynccontextmanager
async def _scoped(factory, tenant):
    session = factory()
    try:
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant})
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


def _asked(cov: dict[str, Any]) -> set[str]:
    return {str(k) for s in (cov.get("states") or [])
            for k in (s.get("form_snapshot_signals") or {})}


# ── INPUT GUARDS. Run without a database, deliberately: a stale or degenerate
#    recording is a problem whether or not a Postgres is available, and these are
#    the assertions that say so before anything downstream can be blamed. ──────

def test_the_recording_matches_the_application_in_the_repository() -> None:
    """The crawls must have been run against the acme-life that ships TODAY."""
    ev = _evidence()
    # NEWLINE-NORMALISED, matching the producer's ``app_sha256``. Raw-byte
    # hashing made the equivalent M2.3 guard fire on a pure CRLF rewrite — a
    # difference no crawl can observe, and a false alarm is how a guard earns
    # itself a `# skip`.
    live = hashlib.sha256(
        ACME_INDEX.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
    assert ev["stamp"]["app_sha256_before"] == live, (
        f"the baseline crawl was recorded against a different {ACME_INDEX.name} "
        f"than the one in the repository now.\n"
        f"  recorded : {ev['stamp']['app_sha256_before']}\n"
        f"  current  : {live}\n"
        f"Re-run the producer half; this evidence is about an application that "
        f"no longer exists.")
    assert ev["stamp"]["app_sha256_after"] != live, (
        "the 'after' recording hashes to the unchanged application — the surgery "
        "did not take effect, so the two crawls saw the same app")


def test_the_two_recordings_differ_by_exactly_the_three_changes() -> None:
    """Guards the input at the level a crawl can state it.

    If anything ELSE appeared or disappeared between the crawls, every
    classification below becomes ambiguous: the diff would be over two different
    walks rather than two versions of one application.
    """
    ev = _evidence()
    stamp = ev["stamp"]
    before, after = _asked(ev["before"]), _asked(ev["after"])

    assert before - after == {stamp["removed_question"]}, (
        f"questions that vanished between the crawls: {sorted(before - after)}; "
        f"expected only {stamp['removed_question']!r}")
    assert after - before == {stamp["added_question"]}, (
        f"questions that appeared between the crawls: {sorted(after - before)}; "
        f"expected only {stamp['added_question']!r}")
    assert stamp["changed_question"] in before & after, (
        f"{stamp['changed_question']!r} is not asked by BOTH crawls, so it "
        f"cannot be the 'changed' one — a question that is only in one of them "
        f"is an addition or a removal")


# ── THE DIFF, AGAINST A REAL POSTGRES ────────────────────────────────────────

@needs_db
def test_three_real_app_changes_produce_three_correct_classifications(capsys) -> None:
    asyncio.run(_run(capsys))


async def _run(capsys) -> None:
    from app.db.models import QecBase
    from app.services import catalog_store, journey_baseline, journey_fold

    ev = _evidence()
    stamp = ev["stamp"]
    removed_label = stamp["removed_question"]
    added_label = stamp["added_question"]
    changed_label = stamp["changed_question"]

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"a21-{uuid.uuid4().hex[:10]}"
    app_id = "acme-life"

    # Redirect every session seam the fold touches at the disposable engine. The
    # house pattern from tests/test_catalog_store.py — the DSN is a superuser, so
    # RLS does not filter and every read below therefore names its tenant.
    originals = {
        m: getattr(m, "tenant_scoped_qec_session")
        for m in (journey_fold, catalog_store, journey_baseline)
        if hasattr(m, "tenant_scoped_qec_session")
    }
    try:
        for module in originals:
            module.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        # ── FOLD 1 — the application as it was ──────────────────────────────
        r1 = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id,
            exploration_id=stamp["baseline_crawl"], coverage=ev["before"])
        assert r1["flows"] > 0 and r1["nodes"] > 0, (
            f"the baseline fold did nothing: {r1}")

        v1 = await catalog_store.build_app_master_catalog(tenant, app_id)
        v1_by_name = {q["name"]: q for q in v1["questions"]}

        for label in (removed_label, changed_label):
            assert label in v1_by_name, (
                f"the baseline catalogue does not contain {label!r}; it holds "
                f"{sorted(v1_by_name)}")
        assert added_label not in v1_by_name, (
            f"the baseline catalogue already contains {added_label!r}, which is "
            f"supposed to arrive with the change")

        removed_qid = v1_by_name[removed_label]["question_id"]
        changed_qid = v1_by_name[changed_label]["question_id"]

        # ── THE APPLICATION CHANGED, THREE WAYS. FOLD 2 ─────────────────────
        r2 = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id,
            exploration_id=stamp["after_crawl"], coverage=ev["after"])
        assert r2["flows"] > 0, f"the second fold did nothing: {r2}"

        # ── THE DIFF ────────────────────────────────────────────────────────
        result = await catalog_store.diff_latest_versions(tenant, app_id)
        diff = result["diff"]
        assert diff is not None, f"no diff was produced: {result}"

        audit = await catalog_store.build_app_master_catalog(
            tenant, app_id, include_retired=True)
        by_id = {q["question_id"]: q for q in audit["questions"]}

        def label_of(qid: str) -> str:
            return by_id.get(qid, {}).get("name", "?")

        v2 = await catalog_store.build_app_master_catalog(tenant, app_id)
        v2_by_name = {q["name"]: q for q in v2["questions"]}
        added_qid = (v2_by_name.get(added_label) or {}).get("question_id", "")

        changed_by_id = {c["question_id"]: c for c in diff["changed"]}

        # ── THE EVIDENCE, PRINTED IN FULL ───────────────────────────────────
        with capsys.disabled():
            print(f"\n{'=' * 72}\nGATE 3 / A21 — CATALOG DIFF ACROSS THREE REAL "
                  f"APPLICATION CHANGES\n{'=' * 72}")
            print(f"app             : acme-life (first-party proving ground)")
            print(f"evidence        : {EVIDENCE}")
            print(f"app sha256      : {stamp['app_sha256_before'][:16]} "
                  f"-> {stamp['app_sha256_after'][:16]}")
            print(f"changes made    : {stamp['surgeries_applied']}")
            print(f"baseline crawl  : {result['from']['crawl_ref']}  "
                  f"({result['from']['question_count']} questions)")
            print(f"second crawl    : {result['to']['crawl_ref']}  "
                  f"({result['to']['question_count']} questions)")

            print("\nADDED:")
            for qid in diff["added"]:
                print(f"  {qid}  {label_of(qid)!r}")
            if not diff["added"]:
                print("  (none)")

            print("\nREMOVED:")
            for entry in diff["removed_detail"]:
                print(f"  {entry['question_id']}  {entry['name']!r}")
                print(f"      lifecycle        : {entry['lifecycle']}")
                print(f"      retired_in_crawl : {entry['retired_in_crawl']}")
                print(f"      last seen in     : {entry['last_seen_crawl']}")
            if not diff["removed_detail"]:
                print("  (none)")

            print("\nCHANGED:")
            for c in diff["changed"]:
                print(f"  {c['question_id']}  {label_of(c['question_id'])!r}  "
                      f"kinds={c['kinds']}")
                for field, move in (c.get("changes") or {}).items():
                    print(f"      {field}: {move['from']!r} -> {move['to']!r}")
            if not diff["changed"]:
                print("  (none)")

            print(f"\nUNCHANGED ({diff['unchanged']}):")
            for qid in diff["unchanged_ids"]:
                print(f"  {qid}  {label_of(qid)!r}")
            print("=" * 72)

        # ── THE THREE CLASSIFICATIONS ───────────────────────────────────────

        # ADDED — exactly the question the application started asking.
        assert added_qid, (
            f"{added_label!r} is not in the catalogue after the second fold, so "
            f"there is nothing for 'added' to name")
        assert diff["added"] == [added_qid], (
            f"added = {[(q, label_of(q)) for q in diff['added']]}; expected "
            f"exactly [{added_qid} {added_label!r}]")

        # REMOVED — exactly the question the application stopped asking, and it
        # is the SAME id the baseline catalogued, not a coincidence of labels.
        assert diff["removed"] == [removed_qid], (
            f"removed = {[(q, label_of(q)) for q in diff['removed']]}; expected "
            f"exactly [{removed_qid} {removed_label!r}]")

        # CHANGED — the identity survived and the answer set moved underneath it.
        assert list(changed_by_id) == [changed_qid], (
            f"changed = {[(q, label_of(q)) for q in changed_by_id]}; expected "
            f"exactly [{changed_qid} {changed_label!r}].\n"
            f"If {changed_label!r} appears in BOTH added and removed instead, "
            f"the change crossed an option-shape bucket and minted a new "
            f"question_id — see the producer's note on why the fixture adds one "
            f"option and not four.")
        entry = changed_by_id[changed_qid]
        assert "options_changed" in entry["kinds"], (
            f"{changed_label!r} is reported as changed but not by its options: "
            f"kinds={entry['kinds']}, changes={entry.get('changes')}")
        move = (entry.get("changes") or {}).get("options") or {}
        gained = set(map(str, move.get("to") or [])) - set(map(str, move.get("from") or []))
        assert {g.strip().lower() for g in gained} == {
            stamp["changed_question_added_option"].strip().lower()}, (
            f"{changed_label!r} options went {move.get('from')} -> {move.get('to')}; "
            f"expected exactly {stamp['changed_question_added_option']!r} to "
            f"arrive")

        # THE CONTROL GROUP, in the catalogue's own terms. Every question that
        # was not one of the three has to land in `unchanged` — otherwise the
        # three classifications above are true but the diff is still noisy, and a
        # reviewer cannot act on it.
        moved = set(diff["added"]) | set(diff["removed"]) | set(changed_by_id)
        for label in stamp["surviving_questions"]:
            qid = (v1_by_name.get(label) or {}).get("question_id")
            assert qid, f"the baseline catalogue never held {label!r}"
            assert qid not in moved, (
                f"{label!r} was not one of the three changes but the diff "
                f"reports it as moved")
            assert qid in set(diff["unchanged_ids"]), (
                f"{label!r} is neither moved nor unchanged — it fell out of the "
                f"diff entirely")
    finally:
        for module, original in originals.items():
            module.tenant_scoped_qec_session = original
        await engine.dispose()
