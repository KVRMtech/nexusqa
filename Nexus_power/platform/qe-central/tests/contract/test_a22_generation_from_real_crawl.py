"""GATE 3 / A22 — the CONSUMER half: a DISCOVERED journey compiles.

    real crawl  →  coverage  →  fold_crawl  →  journey graph  →  build_journey_case

The producer half (``engines/qe-explorer/tests/browser/test_a22_generation_crawl.py``)
drives the production ``Crawler`` in real Chromium against the M2.4 quote funnel and
asserts the crawl walks it and indexes its outcome. This half asserts the rest of what
A22 asks for — *that the journey it discovered compiles into a specification carrying
network and outcome assertions* — by putting that recorded coverage through the
production fold and the production compiler.

WHY IT IS A SEPARATE FILE IN A DIFFERENT SERVICE
================================================
The fold, the journey graph and the compiler live in qe-central, which cannot be
imported into the explorer's process (M1.7 froze that boundary as data). The recorded
coverage account IS the seam, exactly as it is for M2.3, A21 and A24.

WHAT THIS REPLACES, AND WHY IT MATTERS
======================================
``tests/m24_generation/crawl_evidence.py`` — which the M2.4 proof reads — says in its
own first paragraph that its graph rows are FIXTURE: "what a crawl of the quote
application WOULD have recorded". Everything downstream of that account is production
code and genuinely exercised; the account itself is the one thing A22 exists to
replace. This file reads the account a real crawl actually produced.

THE FOUR THINGS ASSERTED, and each one was broken when this was written
======================================================================
1. the walk reached a SECOND state at all (the bare-button wizard gate);
2. the result page is IN the coverage account (it has no questions and no controls,
   and the account was built to discard exactly that);
3. the premium is grounded on a captured SELECTOR (the value rides the flow, the
   selector rides the state — an outcome with neither is "ungrounded" and the
   compiler refuses to guess);
4. the ENTRY page does not claim the RESULT page's premium (`_discover` can navigate
   away before displayed values are read).

THE SOFT ORACLE IS NOT A GAP. ``outcome_oracle: "soft"`` is asserted, not worked
around: T-GEN-04 keeps a crawl-derived outcome non-failing until a human approves the
baseline. Promoting it here — the one thing that would make this test look stronger —
is precisely the green-wash the milestone forbids.
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
    reason="QEC_TEST_DATABASE_URL not set — the A22 generation proof folds a real "
           "discovered crawl into a disposable Postgres",
)


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "proving-grounds").is_dir() and (parent / "engines").is_dir():
            return parent
    raise AssertionError(f"Nexus_power root not found above {here}")


EVIDENCE = _repo_root() / "evidence" / "a22_generation"
PREMIUM_LABEL = "Your monthly premium"
QUOTE_ENDPOINT = "/api/quote"


def _evidence() -> dict[str, Any]:
    missing = [n for n in ("coverage.json", "stamp.json")
               if not (EVIDENCE / n).is_file()]
    assert not missing, (
        f"the A22 crawl evidence is missing {missing} from {EVIDENCE}.\n"
        f"Re-record it by running the producer half:\n"
        f"  cd engines/qe-explorer && pytest tests/browser/test_a22_generation_crawl.py\n"
        f"There is deliberately no fixture fallback — a hand-built account is the "
        f"thing this milestone exists to stop reading.")
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


# ── Runs without a database: a fixture recording is a problem either way ───────

def test_the_evidence_is_a_real_crawl_of_a_backend_calling_application() -> None:
    """A22's whole difficulty is that the journey must be DISCOVERED and the
    application must call a backend. Both are properties of the recording, so both
    are checked before any database is involved."""
    ev = _evidence()
    coverage, stamp = ev["coverage"], ev["stamp"]

    served = [str(s) for s in (stamp.get("server_saw") or [])]
    assert any(s.startswith("POST") and s.endswith(QUOTE_ENDPOINT) for s in served), (
        f"the application's own server log records no POST {QUOTE_ENDPOINT}, so this "
        f"recording cannot ground a network assertion: {served}")

    flows = coverage.get("flows") or []
    assert flows, (
        "the recorded coverage has no flows — the crawl discovered no journey, which "
        "is the A22 blocker itself and not something this consumer can compensate for")
    steps = (flows[0].get("steps") or [])
    assert len(steps) >= 2, (
        f"the discovered journey has {len(steps)} step(s); a journey that never "
        f"advanced has no end-to-end path to compile" + json.dumps(steps)[:400])


@needs_db
def test_a_discovered_journey_folds_and_compiles_with_its_assertions(capsys) -> None:
    asyncio.run(_run(capsys))


async def _run(capsys) -> None:
    from app.db.journey_models import (JourneyEdgeRow, JourneyNodeRow, JourneyRow,
                                       JourneyTraversalRow)
    from app.db.models import QecBase
    from app.services import catalog_store, journey_fold, journey_spec

    ev = _evidence()
    coverage, stamp = ev["coverage"], ev["stamp"]

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"a22-{uuid.uuid4().hex[:10]}"
    app_id = "m24-quote-funnel"

    originals = {
        m: getattr(m, "tenant_scoped_qec_session")
        for m in (journey_fold, catalog_store)
        if hasattr(m, "tenant_scoped_qec_session")
    }
    try:
        for module in originals:
            module.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)

        report = await journey_fold.fold_crawl(
            tenant_id=tenant, app_id=app_id,
            exploration_id=stamp["crawl_id"], coverage=coverage)

        # ── 1 · THE WALK REACHED A SECOND STATE ────────────────────────────
        assert report["nodes"] >= 2, (
            f"the fold produced {report['nodes']} node(s). A one-node graph is what a "
            f"funnel looks like when the walk crossed a navigation while believing it "
            f"stood still — two states collapsed onto one fingerprint: {report}")
        assert report["edges"] >= 1, (
            f"the fold produced no edge, so nothing records WHAT was clicked between "
            f"the two states and no step can be compiled: {report}")

        async with _scoped(factory, tenant) as session:
            nodes = (await session.execute(select(JourneyNodeRow).where(
                JourneyNodeRow.tenant_id == tenant))).scalars().all()
            journeys = (await session.execute(select(JourneyRow).where(
                JourneyRow.tenant_id == tenant))).scalars().all()
            travs = (await session.execute(select(JourneyTraversalRow).where(
                JourneyTraversalRow.tenant_id == tenant))).scalars().all()
            edges = (await session.execute(select(JourneyEdgeRow).where(
                JourneyEdgeRow.tenant_id == tenant))).scalars().all()

        by_url = {n.url: n for n in nodes}
        outcome_nodes = [n for n in nodes if (n.displayed_outcomes or [])]

        # ── 2 + 3 · THE OUTCOME PAGE IS INDEXED, WITH ITS SELECTOR ─────────
        assert outcome_nodes, (
            "no node carries a displayed outcome. The result page has no questions "
            "and no controls, so it is exactly the page the coverage account was "
            "built to discard — and the fold reads that account.")
        grounded = [o for n in outcome_nodes for o in (n.displayed_outcomes or [])
                    if str(o.get("label") or "") == PREMIUM_LABEL
                    and str(o.get("selector") or "")]
        assert grounded, (
            f"the premium reached the graph without a SELECTOR, so a compiled spec "
            f"can only guess where to read it and the compiler will refuse to: "
            f"{[n.displayed_outcomes for n in outcome_nodes]}")

        # ── 4 · AND THE ENTRY PAGE DOES NOT CLAIM IT ──────────────────────
        entry_nodes = [n for n in nodes
                       if not str(n.url or "").endswith("result.html")]
        for node in entry_nodes:
            labels = [str(o.get("label") or "")
                      for o in (node.displayed_outcomes or [])]
            assert PREMIUM_LABEL not in labels, (
                f"node {node.url!r} claims the RESULT page's premium. A discovery "
                f"click that NAVIGATES must not have its landing page's values "
                f"attributed to the state it left: {labels}")

        # ── THE COMPILE, through the production compiler ───────────────────
        assert journeys and travs, f"fold produced no journey/traversal: {report}"
        case = journey_spec.build_journey_case(
            journeys[0], traversal=travs[0],
            nodes_by_fp={n.fingerprint: n for n in nodes},
            edges=edges, tenant_id=tenant,
            endpoint_inventory=coverage.get("endpoint_inventory"))

        assert case.get("compilable") is not False, (
            f"the discovered journey did not compile: {case.get('reason')!r}")
        steps = case.get("steps") or []
        assert len(steps) >= 2, f"compiled {len(steps)} step(s): {json.dumps(steps)[:600]}"

        # THE NETWORK ASSERTION, and it must be RECORDED rather than inferred —
        # a guessed endpoint is a sentence about the application, not evidence.
        recorded = [e for s in steps for e in (s.get("network_expect") or [])
                    if str(e.get("attribution") or "") == "recorded"
                    and str(e.get("path") or "").endswith(QUOTE_ENDPOINT)]
        assert recorded, (
            f"no step carries a RECORDED network assertion for {QUOTE_ENDPOINT}; the "
            f"crawl observed the call, so a compiled spec that cannot assert it has "
            f"lost the evidence between them: {json.dumps(steps)[:800]}")

        # THE OUTCOME IS GROUNDED — nothing ungrounded is tolerated silently.
        assert not (case.get("ungrounded_outcomes") or []), (
            f"the compiler could not ground {case['ungrounded_outcomes']} — it has a "
            f"value with no captured selector and correctly refuses to guess one")

        # AND IT IS HELD SOFT. This is asserted, not worked around: T-GEN-04 keeps a
        # crawl-derived outcome non-failing until a human approves the baseline.
        assert case.get("outcome_oracle") == "soft", (
            f"outcome_oracle is {case.get('outcome_oracle')!r}. A crawl-derived "
            f"outcome must stay soft until a baseline is approved; promoting it here "
            f"would make the specification fail on evidence no one confirmed.")
        assert PREMIUM_LABEL in (case.get("unconfirmed_outcomes") or []), (
            f"the premium is neither grounded-and-unconfirmed nor ungrounded, so it "
            f"has gone missing between the graph and the payload: {case}")

        # ── THE PAYLOAD BECOMES EVIDENCE, so the execution half can read it ──
        #
        # The compile payload is the seam between this proof and the one that RUNS
        # the specification, exactly as `coverage.json` is the seam between the
        # crawl and this proof. It has to be written down for the same reason:
        # the fold needs a database and the Playwright runner needs node, and no
        # one process in this repository has both — `platform/api` (the compiler)
        # and `qe-central` (the fold) each ship a top-level `app` package, so one
        # pytest cannot import both (M1.7).
        #
        # Written UNCONDITIONALLY once the assertions above have passed, so the
        # file on disk is always a payload that satisfied them.
        (EVIDENCE / "compile_payload.json").write_text(
            json.dumps(case, indent=2, sort_keys=True, default=str),
            encoding="utf-8")

        with capsys.disabled():
            print(f"\n{'=' * 72}\nGATE 3 / A22 — A DISCOVERED JOURNEY, COMPILED"
                  f"\n{'=' * 72}")
            print(f"  fold          nodes={report['nodes']} edges={report['edges']} "
                  f"traversals={report['traversals']}")
            print(f"  steps         {[s.get('action') for s in steps]}")
            print(f"  network       {[(e.get('method'), e.get('path'), e.get('attribution')) for e in recorded]}")
            print(f"  outcome       {grounded[0].get('label')!r} -> "
                  f"{grounded[0].get('selector')!r}  oracle={case.get('outcome_oracle')}")
            print(f"  endpoints     asserted={case.get('endpoints_asserted')} "
                  f"recorded_cause={case.get('endpoints_recorded_cause')}")
            print(f"  provenance    {case.get('provenance')}")
            print(f"{'=' * 72}\n")
    finally:
        for module, original in originals.items():
            module.tenant_scoped_qec_session = original
        await engine.dispose()
