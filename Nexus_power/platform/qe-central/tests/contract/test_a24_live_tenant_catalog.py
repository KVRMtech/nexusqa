"""GATE 3 / A24 — the live tenant's capture becomes a PERSISTED catalogue.

    live tenant crawl  →  fold_crawl  →  master catalogue  →  rows in Postgres

The explorer half (``engines/qe-explorer/tests/test_a24_live_tenant_capture.py``)
asserts that the M2.6 capture fixes hold on an application nobody shaped for them.
This half asserts the rest of what A24 asks for — *catalog generation,
persistence, correctness of captured data* — by putting the SAME live-tenant
coverage through the production fold and reading the durable rows back out of a
real database.

WHY IT IS A SEPARATE FILE IN A DIFFERENT SERVICE
================================================
Because the fold, the catalogue and the rows live in qe-central, which cannot be
imported into the explorer's process (M1.7 froze that boundary as data). The
recorded coverage account IS the seam, exactly as it is for M2.3 and A21.

WHAT THE 52-OPTION CONTROL PROVES HERE
======================================
The capture-side test shows the browser and the wire carried all 52 options of
``State of residence``. This one shows the CATALOGUE did too — which is the layer
that used to hold a private ceiling of 48. The claim only closes when the answer
set survives all the way into the durable row a reviewer reads.
"""
from __future__ import annotations

import asyncio
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
    reason="QEC_TEST_DATABASE_URL not set — the A24 live-tenant catalogue proof "
           "folds a real tenant crawl into a disposable Postgres",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "proving-grounds").is_dir() and (parent / "engines").is_dir():
            return parent
    raise AssertionError(f"Nexus_power root not found above {here}")


EVIDENCE = _repo_root() / "evidence" / "a24_live_capture"
LARGE_SELECT = "State of residence"
OLD_CATALOGUE_CEILING = 48


def _evidence() -> dict[str, Any]:
    missing = [n for n in ("coverage.json", "stamp.json")
               if not (EVIDENCE / n).is_file()]
    assert not missing, (
        f"the A24 live-tenant evidence is missing {missing} from {EVIDENCE}.\n"
        f"Re-record it with engines/qe-explorer/record_live_capture.py. There is "
        f"deliberately no fixture fallback.")
    return {
        "coverage": json.loads(
            (EVIDENCE / "coverage.json").read_text(encoding="utf-8")),
        "stamp": json.loads((EVIDENCE / "stamp.json").read_text(encoding="utf-8")),
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


def test_the_evidence_is_a_live_tenant() -> None:
    """Runs without a database: a fixture recording is a problem whether or not
    a Postgres is available."""
    stamp = _evidence()["stamp"]
    target = str(stamp["target_url"])
    assert target.startswith("https://"), (
        f"A24 requires a live tenant over HTTPS; recorded {target!r}")
    assert not target.split("//", 1)[1].startswith(("127.", "localhost")), (
        f"recorded target {target!r} is loopback — a fixture crawl")


@needs_db
def test_a_live_tenant_capture_becomes_a_persisted_catalogue(capsys) -> None:
    asyncio.run(_run(capsys))


async def _run(capsys) -> None:
    from app.db.journey_models import CatalogQuestionRow
    from app.db.models import QecBase
    from app.services import catalog_store, journey_baseline, journey_fold

    ev = _evidence()
    coverage, stamp = ev["coverage"], ev["stamp"]

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"a24-{uuid.uuid4().hex[:10]}"
    app_id = "vkpower-life"

    originals = {
        m: getattr(m, "tenant_scoped_qec_session")
        for m in (journey_fold, catalog_store, journey_baseline)
        if hasattr(m, "tenant_scoped_qec_session")
    }
    try:
        for module in originals:
            module.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        result = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id,
            exploration_id=stamp["crawl_id"], coverage=coverage)
        assert result["nodes"] > 0, (
            f"the fold of a live tenant crawl produced no nodes: {result}")

        catalogue = await catalog_store.build_app_master_catalog(tenant, app_id)
        by_name = {q["name"]: q for q in catalogue["questions"]}

        # THE DURABLE ROWS, read back through the ORM — not the in-memory
        # catalogue the builder just returned. Persistence is the claim.
        async with _scoped(factory, tenant) as session:
            rows = (await session.execute(select(CatalogQuestionRow).where(
                CatalogQuestionRow.tenant_id == tenant,
                CatalogQuestionRow.app_id == app_id,
            ))).scalars().all()
        rows_by_name = {r.name: r for r in rows}

        with capsys.disabled():
            print(f"\n{'=' * 72}\nGATE 3 / A24 — A LIVE TENANT'S CAPTURE, "
                  f"PERSISTED\n{'=' * 72}")
            print(f"tenant app   : {stamp['target_url']}")
            print(f"fold         : {result}")
            print(f"catalogue    : {len(catalogue['questions'])} questions")
            print(f"durable rows : {len(rows)}")
            for row in sorted(rows, key=lambda r: -(len(r.options or []))) [:8]:
                print(f"  {row.name:26} type={row.answer_type:8} "
                      f"options={len(row.options or []):3} "
                      f"options_total={row.options_total:3} "
                      f"rule_state={row.business_rule_state}")
            print("=" * 72)

        assert rows, (
            "the fold reported nodes but no catalogue row reached the database — "
            "generation without persistence is not what A24 asks for")
        assert len(rows) >= 15, (
            f"only {len(rows)} catalogue rows were persisted from a crawl that "
            f"captured 19 distinct controls")

        # THE 52-OPTION CONTROL, ALL THE WAY INTO THE DURABLE ROW.
        assert LARGE_SELECT in rows_by_name, (
            f"{LARGE_SELECT!r} is not among the persisted rows: "
            f"{sorted(rows_by_name)}")
        row = rows_by_name[LARGE_SELECT]
        carried = list(row.options or [])
        assert row.options_total > OLD_CATALOGUE_CEILING, (
            f"{LARGE_SELECT!r} persisted options_total={row.options_total}, at or "
            f"below the old catalogue ceiling of {OLD_CATALOGUE_CEILING} — this "
            f"assertion would pass without proving the ceiling is gone")
        assert len(carried) == row.options_total, (
            f"{LARGE_SELECT!r} persisted {len(carried)} options but counted "
            f"{row.options_total}. The catalogue clipped a live tenant's answer "
            f"set and stored it as complete — the exact defect T-CAP-01 exists "
            f"to prevent.")

        # qec_019's columns are populated by the fold rather than left at their
        # defaults on a real crawl — the migration A20 round-tripped is the one
        # that made these columns exist, and an empty column is a column nobody
        # writes.
        assert row.locator, (
            f"{LARGE_SELECT!r} persisted with no locator, so the catalogue "
            f"cannot point at the control it describes")
        assert row.business_rule_state in ("observed", "UNVERIFIED"), (
            f"unexpected business_rule_state {row.business_rule_state!r}")

        # Every persisted row agrees with the catalogue the builder returned —
        # a row that drifts from its own generator is worse than a missing one.
        for name, question in by_name.items():
            if name in rows_by_name:
                assert rows_by_name[name].question_id == question["question_id"], (
                    f"{name!r} has a different question_id in the durable row "
                    f"than in the generated catalogue")

        _assert_duplicate_identity_is_no_worse_than_measured(rows, capsys)
    finally:
        for module, original in originals.items():
            module.tenant_scoped_qec_session = original
        await engine.dispose()


#: The four controls this live tenant catalogues TWICE, and the id each one's
#: second row is keyed under. Measured, not predicted — see the assertion below.
_KNOWN_DUPLICATED = {
    "Branch of Service": "q_58aea59e5b186336",
    "Coverage Amount": "q_c2eb7ff727af3cee",
    "Military Affiliation": "q_2e5a8e4830d3992e",
    "Term Length": "q_cfbe847f0ca50308",
}


def _assert_duplicate_identity_is_no_worse_than_measured(rows, capsys) -> None:
    """A LIVE TENANT'S CATALOGUE HOLDS THE SAME QUESTION TWICE. Measured, pinned,
    and NOT fixed here — with the root cause named so it can be.

    23 durable rows for 16 distinct question labels. Most of that is correct: this
    application really does ask "First name", "Last name" and "Date of birth" in
    two different places, and the pairs differ in locator (``q_first`` vs
    ``b_first``) and in whether they are required. Two rows is the right answer.

    Four are NOT correct. ``Branch of Service``, ``Coverage Amount``,
    ``Military Affiliation`` and ``Term Length`` each appear twice with the SAME
    DOM id, the SAME type, the SAME option set and the SAME required flag —
    one control, catalogued as two questions.

    THE ROOT CAUSE, established rather than guessed. ``question_id_for`` prefers
    the control SIGNATURE and falls back to the normalised NAME. ``extract_controls``
    merges the signature in from the field ledger keyed by ``(url, name)`` — so a
    control gets its signature-derived id on the page where the walk FILLED it and
    the name-derived id on a page where the walk only OBSERVED it. The proof is
    arithmetic: for each of these four, ``question_id_for({'name': …})`` equals the
    second row's id exactly, and the field ledger holds exactly ONE signature for
    that name across the whole crawl. For ``First name`` and ``Date of birth`` —
    the legitimate pairs — the ledger holds TWO signatures and neither id is the
    name fallback.

    WHY IT IS NOT FIXED HERE. The repair is real and small in shape (resolve the
    signature crawl-wide when a name has exactly one, instead of per-URL), but it
    re-keys ``question_id`` for every control currently sitting on the name
    fallback. That id is the join key for the catalogue, the diff, retirement and
    the committed M2.3 and A21 evidence, and two other sessions are working in
    catalogue and state-identity code right now. Re-keying the catalogue as a
    side effect of an evidence gate is not a decision this milestone gets to make
    alone.

    So it is PINNED instead: exactly these four, no more. If a fifth question
    starts duplicating, or one of these stops, this goes red and a human decides —
    rather than the number quietly drifting.
    """
    by_name: dict[str, list] = {}
    for row in rows:
        by_name.setdefault(row.name, []).append(row)
    duplicated = {n: rs for n, rs in by_name.items() if len(rs) > 1}

    same_control = {}
    for name, rs in duplicated.items():
        locators = {(r.locator or {}).get("value") for r in rs}
        if len(locators) == 1:                       # one control, two rows
            same_control[name] = sorted(r.question_id for r in rs)

    with capsys.disabled():
        print(f"\nDUPLICATE CATALOGUE IDENTITY on this live tenant:")
        print(f"  rows={len(rows)}  distinct labels={len(by_name)}  "
              f"labels with >1 row={len(duplicated)}")
        for name, rs in sorted(duplicated.items()):
            locs = sorted({str((r.locator or {}).get("value")) for r in rs})
            verdict = ("SAME CONTROL, TWO IDS — defect"
                       if len(locs) == 1 else "two genuinely different controls")
            print(f"  {name:24} locators={locs}  -> {verdict}")
        print()

    assert set(same_control) == set(_KNOWN_DUPLICATED), (
        f"the set of questions this tenant catalogues twice under ONE control has "
        f"changed.\n  measured now : {sorted(same_control)}\n"
        f"  pinned       : {sorted(_KNOWN_DUPLICATED)}\n"
        f"If it grew, the per-URL signature merge is duplicating more of the "
        f"application. If it shrank, the fix landed and this pin should be "
        f"removed along with the four ids below.")
    for name, expected_id in _KNOWN_DUPLICATED.items():
        assert expected_id in same_control[name], (
            f"{name!r} no longer carries the name-fallback id {expected_id}; its "
            f"ids are {same_control[name]}. The identity rule changed — re-derive "
            f"the root cause before re-pinning.")
