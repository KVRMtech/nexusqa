"""M2.3 / T-ST-04 — THE CONSUMER HALF: two real crawls become a catalogue that
retires a question, and a diff that names it.

    real coverage (baseline)  →  fold_crawl  →  catalog version 1
    real coverage (after the app lost a question)  →  fold_crawl  →  version 2
    →  diff  →  removed = [the question that disappeared]

WHAT IS REAL HERE, AND WHAT IS NOT
==================================
The two inputs are the coverage accounts of two REAL crawls: real Chromium,
through the production ``app.crawler.Crawler``, against ``proving-grounds/
acme-life`` — the second one crawled after the question was genuinely deleted
from the application's source. They are produced by
``engines/qe-explorer/tests/browser/test_catalog_retirement_regression.py`` and
land in ``Nexus_power/evidence/m23_retirement/``.

They arrive as files rather than as function calls because the two services
cannot share a process: both ship a top-level ``app`` package, and M1.7
established that their contracts have to be frozen as data. The artifact IS the
boundary. What runs on this side is entirely production code — ``fold_crawl``,
``persist_catalog_version``, ``build_master_catalog``, ``diff_latest_versions``
— against a real Postgres with the real schema.

THE ANTI-FOSSIL GUARD
=====================
A committed recording could go stale, and a stale recording that still passes is
worse than no test. So the producer stamps the SHA-256 of the acme-life source it
crawled, and :func:`test_the_recording_matches_the_application_in_the_repository`
re-hashes the file as it is now. Edit the proving ground and this module goes red
until the crawls are re-run — it cannot quietly keep proving something about an
application that no longer exists.

DB-gated in the house pattern: skips on a laptop with no ``QEC_TEST_DATABASE_URL``,
fails loudly under ``QEC_REQUIRE_DB``.
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
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — the M2.3 retirement regression folds "
           "two real crawls into a disposable Postgres",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "proving-grounds").is_dir() and (parent / "engines").is_dir():
            return parent
    raise AssertionError(f"Nexus_power root not found above {here}")


ROOT = _repo_root()
EVIDENCE = ROOT / "evidence" / "m23_retirement"
ACME_INDEX = ROOT / "proving-grounds" / "acme-life" / "index.html"


def _evidence() -> dict[str, Any]:
    """The two real coverage accounts plus the producer's stamp."""
    missing = [n for n in ("stamp.json", "coverage_baseline.json",
                           "coverage_after_removal.json")
               if not (EVIDENCE / n).is_file()]
    assert not missing, (
        f"the M2.3 crawl evidence is missing {missing} from {EVIDENCE}.\n"
        f"Regenerate it with the producer half:\n"
        f"  cd engines/qe-explorer && python -m pytest "
        f"tests/browser/test_catalog_retirement_regression.py\n"
        f"This module deliberately does NOT fall back to a hand-written fixture: "
        f"the milestone's whole claim is that a real application change produces "
        f"a real retirement, and a fixture cannot make that claim.")
    return {
        "stamp": json.loads((EVIDENCE / "stamp.json").read_text(encoding="utf-8")),
        "before": json.loads(
            (EVIDENCE / "coverage_baseline.json").read_text(encoding="utf-8")),
        "after": json.loads(
            (EVIDENCE / "coverage_after_removal.json").read_text(encoding="utf-8")),
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


def test_the_recording_matches_the_application_in_the_repository() -> None:
    """The crawls must have been run against the acme-life that ships TODAY.

    Runs without a database on purpose: a stale recording is a problem whether or
    not a Postgres is available, and this is the assertion that says so.
    """
    ev = _evidence()
    # NEWLINE-NORMALISED, matching the producer's ``app_sha256``. Raw-byte
    # hashing made this guard fire on a pure CRLF rewrite of acme-life — a
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
        "the 'after' recording hashes to the unchanged application — the removal "
        "surgery did not take effect, so the two crawls saw the same app")


def test_the_two_recordings_really_differ_by_one_question() -> None:
    """Guards the input, before anything downstream can be blamed for it."""
    ev = _evidence()
    removed = ev["stamp"]["removed_question"]

    def asked(cov):
        return {str(k) for s in (cov.get("states") or [])
                for k in (s.get("form_snapshot_signals") or {})}

    before, after = asked(ev["before"]), asked(ev["after"])
    assert removed in before, f"the baseline never asked {removed!r}"
    assert removed not in after, f"the second crawl still asks {removed!r}"
    assert before - after == {removed}, (
        f"the two crawls differ by more than the removed question: "
        f"{sorted(before - after)}. Anything else that stopped being asked would "
        f"make the diff below ambiguous.")


@needs_db
def test_a_real_app_change_retires_a_real_catalogue_question(capsys) -> None:
    asyncio.run(_run(capsys))


async def _run(capsys) -> None:
    from app.db.journey_models import CatalogQuestionRow, JourneyNodeRow
    from app.db.models import QecBase
    from app.services import catalog_store, journey_baseline, journey_fold
    from app.services.catalog import LIFECYCLE_RETIRED, question_id_for

    ev = _evidence()
    stamp = ev["stamp"]
    removed_label = stamp["removed_question"]

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"m23-{uuid.uuid4().hex[:10]}"
    app_id = "acme-life"

    # Redirect every session seam the fold touches at the disposable engine. The
    # house pattern from tests/test_catalog_store.py — the DSN is a superuser, so
    # RLS does not filter, and every read below therefore names its tenant.
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
        assert r1["flows"] > 0 and r1["nodes"] > 0, f"the baseline fold did nothing: {r1}"

        v1 = await catalog_store.build_app_master_catalog(tenant, app_id)
        v1_by_name = {q["name"]: q for q in v1["questions"]}
        assert removed_label in v1_by_name, (
            f"the baseline catalogue does not contain {removed_label!r}; it holds "
            f"{sorted(v1_by_name)}")
        target_qid = v1_by_name[removed_label]["question_id"]

        # ── THE APPLICATION CHANGED. FOLD 2 ────────────────────────────────
        r2 = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id,
            exploration_id=stamp["after_crawl"], coverage=ev["after"])
        assert r2["flows"] > 0, f"the second fold did nothing: {r2}"

        # ── THE DIFF ────────────────────────────────────────────────────────
        result = await catalog_store.diff_latest_versions(tenant, app_id)
        diff = result["diff"]
        assert diff is not None, f"no diff was produced: {result}"

        active = await catalog_store.build_app_master_catalog(tenant, app_id)
        audit = await catalog_store.build_app_master_catalog(
            tenant, app_id, include_retired=True)
        retired_rows = await catalog_store.load_retired_questions(tenant, app_id)

        async with _scoped(factory, tenant) as session:
            row = (await session.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant,
                CatalogQuestionRow.app_id == app_id,
                CatalogQuestionRow.question_id == target_qid,
            ))).scalar_one_or_none()
            node_count = len((await session.execute(select(JourneyNodeRow).where(
                JourneyNodeRow.tenant_id == tenant,
                JourneyNodeRow.app_id == app_id))).scalars().all())

        # ── THE EVIDENCE, printed in full ───────────────────────────────────
        by_id = {q["question_id"]: q for q in audit["questions"]}

        def label(qid):
            return by_id.get(qid, {}).get("name", "?")

        with capsys.disabled():
            print(f"\n{'=' * 72}\nM2.3 T-ST-04 — CATALOG DIFF ACROSS A REAL "
                  f"APPLICATION CHANGE\n{'=' * 72}")
            print(f"app            : acme-life (first-party proving ground)")
            print(f"change         : deleted the {removed_label!r} question")
            print(f"baseline crawl : {result['from']['crawl_ref']}  "
                  f"({result['from']['question_count']} questions)")
            print(f"second crawl   : {result['to']['crawl_ref']}  "
                  f"({result['to']['question_count']} questions)")
            print(f"nodes in graph : {node_count}")
            print("\nadded:")
            for qid in diff["added"]:
                print(f"  {qid}  {label(qid)!r}")
            if not diff["added"]:
                print("  (none)")
            print("\nremoved:")
            for entry in diff["removed_detail"]:
                print(f"  {entry['question_id']}  {entry['name']!r}")
                print(f"      lifecycle        : {entry['lifecycle']}")
                print(f"      stale            : {entry['stale']}")
                print(f"      retired_at       : {entry['retired_at']}")
                print(f"      retired_in_crawl : {entry['retired_in_crawl']}")
                print(f"      retire_reason    : {entry['retire_reason']}")
                print(f"      last seen in     : {entry['last_seen_crawl']}")
                print(f"      first seen in    : {entry['first_seen_crawl']}")
                print(f"      was asked on     : {entry['pages']}")
            if not diff["removed_detail"]:
                print("  (none)")
            print("\nchanged:")
            for c in diff["changed"]:
                print(f"  {c['question_id']}  {label(c['question_id'])!r}  "
                      f"{c['kinds']}")
            if not diff["changed"]:
                print("  (none)")
            print(f"\nunchanged ({diff['unchanged']}):")
            for qid in diff["unchanged_ids"]:
                print(f"  {qid}  {label(qid)!r}")
            print(f"\nactive catalogue : {len(active['questions'])} questions"
                  f"   (summary: {active['summary']['active_count']} active, "
                  f"{active['summary']['stale_count']} stale, "
                  f"{active['summary']['retired_count']} retired)")
            print(f"audit catalogue  : {len(audit['questions'])} questions "
                  f"(nothing deleted)")
            print(f"GET .../catalog/retired : {len(retired_rows)} row(s)")
            print("=" * 72)

        # ── T-ST-03: the removed bucket names the question that disappeared ──
        assert target_qid in diff["removed"], (
            f"{removed_label!r} ({target_qid}) is not in the diff's removed "
            f"bucket. removed={diff['removed']}")
        assert len(diff["removed"]) == 1, (
            f"more than the removed question left the catalogue: "
            f"{[(q, label(q)) for q in diff['removed']]}")
        detail = next(d for d in diff["removed_detail"]
                      if d["question_id"] == target_qid)
        assert detail["name"] == removed_label

        # ── T-ST-01/T-ST-02: the historical row, kept and stamped ───────────
        assert row is not None, (
            "the retired question has NO durable row — it was deleted, and the "
            "milestone's first requirement is that a question never silently "
            "disappears")
        assert row.question_id == target_qid, "the historical question id moved"
        assert row.name == removed_label, "the retired row lost its content"
        assert row.stale is True, "the retired row is not marked stale"
        assert row.retired_at is not None, "no retirement timestamp"
        assert row.retired_in_crawl == stamp["after_crawl"], (
            f"the retirement names {row.retired_in_crawl!r}, not the crawl that "
            f"established it ({stamp['after_crawl']!r})")
        assert row.retire_reason == "conclusive_absence", (
            f"unexpected retirement evidence: {row.retire_reason!r}")
        assert row.first_seen_artifact == stamp["baseline_crawl"], (
            "the retired row lost its first-seen record")
        assert row.last_seen_crawl == stamp["baseline_crawl"], (
            f"last_seen_crawl is {row.last_seen_crawl!r}; it must name the last "
            f"crawl that ACTUALLY observed the question, not the one that "
            f"retired it")

        # ── EXCLUDED FROM ACTIVE PLANNING ───────────────────────────────────
        assert target_qid not in {q["question_id"] for q in active["questions"]}, (
            "the retired question is still in the ACTIVE catalogue, which is what "
            "planning and scenario derivation read")
        assert active["summary"]["retired_count"] >= 1, (
            "the active catalogue does not declare that anything is being "
            "withheld from it")

        # ── STILL VISIBLE FOR AUDIT ─────────────────────────────────────────
        audited = by_id.get(target_qid)
        assert audited is not None, "the audit catalogue lost the retired question"
        assert audited["lifecycle"] == LIFECYCLE_RETIRED
        assert target_qid in {r["question_id"] for r in retired_rows}, (
            "the retirement audit route does not list the question")

        # ── THE CONTROL GROUP: everything else is UNCHANGED ─────────────────
        for surviving in stamp["surviving_questions"]:
            qid = v1_by_name[surviving]["question_id"]
            assert qid in diff["unchanged_ids"], (
                f"{surviving!r} was not removed from the application but the diff "
                f"does not report it as unchanged. A diff that moves questions "
                f"nobody touched cannot be trusted about the one that moved.")
            assert qid in {q["question_id"] for q in active["questions"]}, (
                f"{surviving!r} fell out of the active catalogue")
        # ── WHAT ELSE THE DIFF REPORTS, AND WHY ─────────────────────────────
        # One row is added: a second "next action" fork. That is not a question
        # anyone answers — it is the WALKER'S OWN record of which control it
        # chose, catalogued as a branch question, and its signature is literally
        # `nextaction:<hash of the page state>`. Measured across these two
        # crawls it moved from nextaction:4bc9dd780 to nextaction:43d4c19ce
        # because the application form's structure changed, while the review
        # page's fork (nextaction:b21cd922f) did not move at all.
        #
        # So this is reported, not asserted away, and the limit it exposes is
        # named rather than hidden: BRANCH-SOURCED questions on a node whose
        # identity changed are not superseded the way node CONTROLS are (see
        # `journey_fold._superseding_observation`), so the apply page's previous
        # fork stays active alongside its replacement. Node controls carry every
        # text, date, number and select question in an application and that path
        # is proven end to end above; extending supersession to the branch key
        # space is a further change, and inventing evidence for it here would be
        # exactly the green-wash this milestone exists to remove.
        business_added = [q for q in diff["added"]
                          if by_id.get(q, {}).get("source") != "branch"]
        assert not business_added, (
            f"the application gained no question a user answers, but the diff "
            f"reports {[(q, label(q)) for q in business_added]} as added")
        assert all(label(q).strip().lower() == "next action" for q in diff["added"]), (
            f"an added branch question that is NOT a walker next-action fork: "
            f"{[(q, label(q)) for q in diff['added']]}. Anything else added by a "
            f"pure REMOVAL is a defect, not a re-identified fork.")
    finally:
        for module, original in originals.items():
            module.tenant_scoped_qec_session = original
        await engine.dispose()
