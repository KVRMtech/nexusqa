"""QE-Central Phase-5 — certified-invariant executor tests (pure + fake factory).

Pins the honesty spine of :mod:`app.services.invariant_executor`:

  * FAIL-CLOSED disposable-env gate: a missing / wrong-kind / expired attestation
    ⇒ ``blocked`` and the factory is NEVER called (no mutating submit can touch a
    non-disposable env) — AND it cannot be bypassed by ``disposable_required=False``
    when the invariant itself requires a disposable env;
  * a MUST-REFUSE proof is mandatory: a positive-green run + a break-mode that is
    correctly refused (+ a real confirmation baseline) ⇒ ``certified`` /
    ``behaves=True``; a break-mode that STAYS green is a GREEN_WASH ⇒ ``error``,
    NEVER ``certified``;
  * a positive that only certifies STATIC (no CERTIFIED-EVIDENCED baseline) is
    ``refused`` (RENDERS, not BEHAVES);
  * no break-probe configured / no linked artifact ⇒ ``refused`` before any run.

Pure-logic with an injected fake factory + fake loader — no network, no DB.  The
DB loader path is skipif-gated on ``QEC_TEST_DATABASE_URL``.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import invariant_executor as ie
from app.services.invariant_executor import (
    STATUS_BLOCKED,
    STATUS_CERTIFIED,
    STATUS_ERROR,
    STATUS_REFUSED,
    AttestationCheck,
    InvariantContext,
    check_disposable_attestation,
    decide_certification,
    execute_invariant,
    resolve_break_probe,
    resolve_positive_base_url,
    verify_has_confirmation_baseline,
    verify_is_certified_green,
)
from app.db import utc_now


# ── verdict fixtures (the VKPower /verify shape, test_factory.py:4685-4707) ──
GREEN_EVIDENCED = {
    "decision": "certified", "certification_level": "CERTIFIED-EVIDENCED",
    "readiness": {"status": "READY"},
}
GREEN_STATIC = {
    "decision": "certified", "certification_level": "CERTIFIED-STATIC",
    "readiness": {"status": "READY"},
}
BROKEN = {
    "decision": "REPAIR", "certification_level": "REPAIR",
    "readiness": {"status": "BLOCKED"},
}

_BREAK_URL = "https://disposable.example/?break=underwriting_ceiling"
_POS_URL = "https://disposable.example"


def _future_iso(hours: int = 1) -> str:
    return (utc_now() + timedelta(hours=hours)).isoformat()


def _past_iso(hours: int = 1) -> str:
    return (utc_now() - timedelta(hours=hours)).isoformat()


def _ctx(**overrides) -> InvariantContext:
    base = dict(
        invariant_id="inv1",
        tenant_id="t1",
        app_id="a1",
        statement="premium never exceeds the underwriting ceiling",
        band="P0",
        status="certified",
        requires_disposable_env=True,
        linked_scenario_ids=("sc1",),
        env_attestation={
            "env_kind": "disposable",
            "expires_at": _future_iso(),
            "base_url": _POS_URL,
            "break_probe": {"base_url": _BREAK_URL},
        },
        fences={},
        base_url=_POS_URL,
        scenario_artifact_id="art-mat",
    )
    base.update(overrides)
    return InvariantContext(**base)


def _loader(ctx):
    async def _load(*, tenant_id, invariant_id):
        return ctx
    return _load


class _FakeFactory:
    """A fake :class:`InvariantFactory` that records EVERY call and returns a
    canned verdict keyed by base_url (positive vs break)."""

    def __init__(self, *, positive, break_, test_ids=("t1",), generated=None):
        self.positive = positive
        self.break_ = break_
        self.test_ids = list(test_ids)
        self.generated = generated if generated is not None else {
            "success": True, "generated": len(test_ids),
        }
        self.calls: list = []

    async def generate(self, *, tenant_id, artifact_id):
        self.calls.append(("generate", artifact_id))
        return self.generated

    async def get_rtm(self, *, tenant_id, artifact_id):
        self.calls.append(("get_rtm", artifact_id))
        return {"artifact_id": artifact_id,
                "tests": [{"test_id": t} for t in self.test_ids]}

    async def verify(self, *, tenant_id, artifact_id, test_id, base_url):
        self.calls.append(("verify", test_id, base_url))
        verdict = self.break_ if base_url == _BREAK_URL else self.positive
        return {**verdict, "test_id": test_id}

    def verify_calls(self):
        return [c for c in self.calls if c[0] == "verify"]


def _run(ctx, factory, **kw):
    return asyncio.run(execute_invariant(
        "t1", "inv1", factory=factory, loader=_loader(ctx), **kw,
    ))


# ══════════════════════ pure: attestation gate ═════════════════════════════

def test_attestation_ok_when_disposable_and_unexpired():
    chk = check_disposable_attestation(
        {"env_kind": "disposable", "expires_at": _future_iso()},
        now=utc_now(), required=True,
    )
    assert chk.ok is True and chk.expired is False


def test_attestation_blocks_non_disposable_env():
    chk = check_disposable_attestation(
        {"env_kind": "staging", "expires_at": _future_iso()},
        now=utc_now(), required=True,
    )
    assert chk.ok is False and "disposable" in chk.reason


def test_attestation_blocks_expired():
    chk = check_disposable_attestation(
        {"env_kind": "disposable", "expires_at": _past_iso()},
        now=utc_now(), required=True,
    )
    assert chk.ok is False and chk.expired is True


def test_attestation_blocks_missing_expiry():
    chk = check_disposable_attestation(
        {"env_kind": "disposable"}, now=utc_now(), required=True,
    )
    assert chk.ok is False


def test_attestation_not_required_is_a_pass():
    chk = check_disposable_attestation({}, now=utc_now(), required=False)
    assert chk.ok is True and chk.required is False


# ══════════════════════ pure: URL + verdict readers ════════════════════════

def test_resolve_break_probe_base_url_form():
    url, cfg = resolve_break_probe(
        {"break_probe": {"base_url": _BREAK_URL}}, {}, _POS_URL,
    )
    assert url == _BREAK_URL


def test_resolve_break_probe_param_form_appends():
    url, cfg = resolve_break_probe(
        {}, {"break_probe": {"param": "break", "value": "x"}}, "https://z.example",
    )
    assert url == "https://z.example?break=x"


def test_resolve_break_probe_none_when_unconfigured():
    url, cfg = resolve_break_probe({}, {}, _POS_URL)
    assert url == "" and cfg == {}


def test_positive_base_url_prefers_attestation():
    ctx = _ctx(base_url="https://app.example",
               env_attestation={"base_url": "https://disp.example"})
    assert resolve_positive_base_url(ctx) == "https://disp.example"


def test_verify_certified_green_and_baseline_predicates():
    assert verify_is_certified_green(GREEN_EVIDENCED) is True
    assert verify_is_certified_green(GREEN_STATIC) is True     # certified + READY
    assert verify_is_certified_green(BROKEN) is False
    assert verify_has_confirmation_baseline(GREEN_EVIDENCED) is True
    assert verify_has_confirmation_baseline(GREEN_STATIC) is False  # STATIC ≠ baseline


# ══════════════════════ pure: decide_certification ═════════════════════════

def _decide(positive, break_):
    return decide_certification(
        invariant_id="inv1", statement="s", band="P0",
        positive_verdicts=positive, break_verdicts=break_,
        positive_url=_POS_URL, break_url=_BREAK_URL,
    )


def test_decide_certified_when_positive_green_and_break_refused():
    r = _decide([GREEN_EVIDENCED], [BROKEN])
    assert r.status == STATUS_CERTIFIED and r.behaves is True
    assert r.refuse_proof["green_wash"] is False
    assert r.refuse_proof["break_refused"] is True
    assert r.refuse_proof["baseline_present"] is True


def test_decide_green_wash_when_break_stays_green_is_never_certified():
    r = _decide([GREEN_EVIDENCED], [GREEN_EVIDENCED])
    assert r.status == STATUS_ERROR and r.behaves is False
    assert r.status != STATUS_CERTIFIED
    assert r.refuse_proof["green_wash"] is True


def test_decide_refused_without_confirmation_baseline():
    # Positive certifies STATIC (rubric pass, no grounded evidence) — RENDERS only.
    r = _decide([GREEN_STATIC], [BROKEN])
    assert r.status == STATUS_REFUSED and r.behaves is False
    assert r.refuse_proof["baseline_present"] is False


def test_decide_refused_when_positive_not_green():
    r = _decide([BROKEN], [BROKEN])
    assert r.status == STATUS_REFUSED and r.behaves is False


def test_decide_refused_without_a_proof():
    r = _decide([], [])
    assert r.status == STATUS_REFUSED and r.behaves is False


def test_decide_refused_when_any_positive_case_not_green():
    r = _decide([GREEN_EVIDENCED, BROKEN], [BROKEN])
    assert r.status == STATUS_REFUSED
    assert r.refuse_proof["positive_certified"] is False


# ══════════════════════ execute_invariant (fake factory + loader) ══════════

def test_blocked_missing_attestation_never_calls_factory():
    ctx = _ctx(env_attestation={"env_kind": "staging",
                                "break_probe": {"base_url": _BREAK_URL}})
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN)
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_BLOCKED and result.behaves is False
    assert result.refuse_proof["factory_called"] is False
    assert fake.calls == []  # a mutating run NEVER reached the factory


def test_blocked_cannot_be_bypassed_by_disposable_required_false():
    # The invariant itself requires a disposable env — passing
    # disposable_required=False must NOT open a mutating run on a non-disposable env.
    ctx = _ctx(requires_disposable_env=True,
               env_attestation={"env_kind": "prod",
                                "break_probe": {"base_url": _BREAK_URL}})
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN)
    result = _run(ctx, fake, disposable_required=False)
    assert result.status == STATUS_BLOCKED
    assert fake.calls == []


def test_certified_positive_green_and_break_refused():
    ctx = _ctx()
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN, test_ids=("t1", "t2"))
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_CERTIFIED and result.behaves is True
    assert result.refuse_proof["green_wash"] is False
    assert result.refuse_proof["factory_called"] is True
    # generate + get_rtm + verify(2 tests × 2 envs)
    assert ("generate", "art-mat") in fake.calls
    assert len(fake.verify_calls()) == 4


def test_green_wash_break_stays_green_is_flagged_never_certified():
    ctx = _ctx()
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=GREEN_EVIDENCED)
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_ERROR and result.behaves is False
    assert result.status != STATUS_CERTIFIED
    assert result.refuse_proof["green_wash"] is True


def test_refused_when_positive_is_static_only():
    ctx = _ctx()
    fake = _FakeFactory(positive=GREEN_STATIC, break_=BROKEN)
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_REFUSED and result.behaves is False
    assert result.refuse_proof["baseline_present"] is False


def test_refused_without_break_probe_never_runs_factory():
    ctx = _ctx(env_attestation={"env_kind": "disposable",
                                "expires_at": _future_iso(), "base_url": _POS_URL})
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN)
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_REFUSED
    assert result.refuse_proof["attempted"] is False
    assert fake.calls == []  # attested but no proof configured — never generate/run


def test_refused_without_linked_artifact():
    ctx = _ctx(scenario_artifact_id="")
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN)
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_REFUSED
    assert fake.calls == []


def test_not_required_proceeds_without_attestation():
    # Not a submit-tier requirement: no disposable attestation needed, the run
    # proceeds and certifies on the merits.
    ctx = _ctx(
        requires_disposable_env=False,
        env_attestation={"base_url": _POS_URL, "break_probe": {"base_url": _BREAK_URL}},
    )
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN)
    result = _run(ctx, fake, disposable_required=False)
    assert result.status == STATUS_CERTIFIED
    assert fake.calls  # the factory WAS driven


def test_generate_produces_no_cases_is_refused():
    ctx = _ctx()
    fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN, test_ids=())
    result = _run(ctx, fake, disposable_required=True)
    assert result.status == STATUS_REFUSED
    # generate + get_rtm ran; no verify (no cases)
    assert fake.verify_calls() == []


# ══════════════════════ DB loader (skipif-gated) ═══════════════════════════

DB_URL = os.environ.get("QEC_TEST_DATABASE_URL", "")
needs_db = pytest.mark.skipif(
    not DB_URL,
    reason="QEC_TEST_DATABASE_URL not set — load_invariant_context needs a "
           "disposable Postgres (QecBase tables are created in-test)",
)


@asynccontextmanager
async def _scoped(factory, tenant):
    session = factory()
    try:
        await session.execute(
            text("SELECT set_config('nexus.current_tenant_id', :t, true)"),
            {"t": tenant},
        )
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


@needs_db
def test_load_invariant_context_resolves_app_and_scenario_artifact():
    asyncio.run(_run_load())


async def _run_load():
    from app.db.gov_models import CertifiedInvariantRow, ScenarioRow
    from app.db.models import ClientAppRow, QecBase

    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(QecBase.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = f"qec-inv-{uuid.uuid4().hex[:10]}"
    original = ie.tenant_scoped_qec_session
    try:
        ie.tenant_scoped_qec_session = lambda tid: _scoped(factory, tid)
        async with _scoped(factory, tenant) as s:
            s.add(ClientAppRow(
                app_id="a1", tenant_id=tenant, name="Acme", base_url=_POS_URL,
                canonical_host="example",
                env_attestation={"env_kind": "disposable", "expires_at": _future_iso(),
                                 "break_probe": {"base_url": _BREAK_URL}},
                fences={},
            ))
            s.add(ScenarioRow(
                scenario_id="sc1", tenant_id=tenant, app_id="a1",
                source_artifact_id="art-src", materialized_artifact_id="art-mat",
            ))
            s.add(CertifiedInvariantRow(
                invariant_id="inv1", tenant_id=tenant, app_id="a1",
                statement="ceiling", criticality_band="P0", signature="Jane QA",
                signed_by="Jane QA", requires_disposable_env=True,
                linked_scenario_ids=["sc1"], status="certified",
            ))

        ctx = await ie.load_invariant_context(tenant_id=tenant, invariant_id="inv1")
        assert ctx is not None
        assert ctx.app_id == "a1"
        assert ctx.scenario_artifact_id == "art-mat"     # materialized preferred
        assert ctx.env_attestation["env_kind"] == "disposable"
        assert ctx.requires_disposable_env is True
        assert ctx.linked_scenario_ids == ("sc1",)

        # A valid disposable attestation → the executor certifies with a fake factory.
        fake = _FakeFactory(positive=GREEN_EVIDENCED, break_=BROKEN)
        result = await execute_invariant("ignored", "inv1", factory=fake,
                                         loader=lambda **kw: ie.load_invariant_context(
                                             tenant_id=tenant, invariant_id="inv1"))
        assert result.status == STATUS_CERTIFIED

        missing = await ie.load_invariant_context(tenant_id=tenant, invariant_id="nope")
        assert missing is None
    finally:
        ie.tenant_scoped_qec_session = original
        await engine.dispose()
