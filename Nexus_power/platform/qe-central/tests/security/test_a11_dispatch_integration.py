"""A11.4 / A11.5 — THE DISPATCH SEAM: how a proof reaches a crawl worker, and
what cannot ride along with it.

THE PLATFORM CONTRACT THIS FILE DEFENDS
=======================================
``walk_attested`` may be set ONLY as the result of a successful cryptographic
verification.  Not by a dispatch flag, not by client metadata, not by a config
value, not by an API parameter.  Attaching a proof to a dispatch REQUESTS
mutation authority; it never grants it.

That contract has two halves and they live in two services:

  * qe-central (here) may only ever ATTACH bytes.  It has no way to set
    ``walk_attested`` and must not acquire one — asserted below by reading the
    dispatch payload the explorer would actually receive.
  * qe-explorer derives ``walk_attested`` from its OWN verification verdict
    (``guard_context``/``main._walk_authorization``).  Its own suite owns that
    half.

WHY THE ENVIRONMENT IS NOT A REQUEST PARAMETER
==============================================
``_walk_attestation_for_dispatch`` finds the provisioning record by matching the
crawl's own ``base_url`` origin against the origin a PLATFORM ADMIN pinned.
There is deliberately no ``environment_id`` on the crawl API: a caller-supplied
identifier would be one more tenant-controlled input on the path to mutation
authority.  Several tests below poke at that seam.

RETURNING ``None`` IS THE SAFE, ORDINARY ANSWER, and most of this file is about
proving that every failure mode reaches it.  No proof means the verifier denies
with ``no_proof`` and the crawl catalogues without persisting — precisely the
behaviour that existed before this milestone.
"""
from __future__ import annotations

import pytest

from _a11_kit import (
    APP,
    ENV,
    ORIGIN,
    TENANT,
    FakeSession,
    attest,
    bootstrap_issuer_key,
    make_environment_row,
    make_envelope_service,
    make_provisioning_record,
    trust_store,
    verify,
)

from app.routers import explorations
from app.services import attestation_revocation as revocation


@pytest.fixture
def envelope(tmp_path):
    return make_envelope_service(tmp_path)


@pytest.fixture
def world():
    s = FakeSession()
    s.seed(make_provisioning_record())
    s.seed(make_environment_row())
    return s


@pytest.fixture
async def key(world, envelope):
    return await bootstrap_issuer_key(world, envelope)


async def _dispatch_envelope(world, envelope, *, origin=ORIGIN,
                             crawl_id="crawl-dispatch-1", app_id=APP,
                             tenant_id=TENANT):
    return await explorations._issue_walk_proof(
        world, envelope, tenant_id=tenant_id, app_id=app_id,
        crawl_id=crawl_id, origin=origin, actor="dispatcher@nexus.test")


# ── the attachment itself ───────────────────────────────────────────────────

async def test_dispatch_attaches_a_proof_the_real_verifier_accepts(
        world, envelope, key):
    """A11.4 END-TO-END: certified environment → dispatch attaches → the REAL
    explorer verifier authorises walk persistence for that crawl."""
    attached = await _dispatch_envelope(world, envelope)
    assert attached is not None
    assert set(attached) == {"proof", "revocations"}

    verdict = verify(attached, trust=trust_store([key]),
                     crawl_id="crawl-dispatch-1")
    assert verdict.authorized is True
    assert verdict.reason == attest.AttestReason.OK


async def test_the_attached_proof_is_bound_to_this_dispatchs_crawl(
        world, envelope, key):
    """The binding is what stops a proof lifted from one dispatch working on
    another, so it must actually track the crawl the dispatcher is minting."""
    attached = await _dispatch_envelope(world, envelope, crawl_id="crawl-xyz")
    trust = trust_store([key])

    assert verify(attached, trust=trust, crawl_id="crawl-xyz").authorized is True
    other = verify(attached, trust=trust, crawl_id="crawl-other")
    assert other.authorized is False
    assert other.reason == attest.AttestReason.CRAWL_BINDING_MISMATCH


# ── every path to "no proof", which is always safe ──────────────────────────

async def test_an_uncertified_origin_attaches_nothing(world, envelope, key):
    """The ordinary case — true of every production crawl, forever."""
    assert await _dispatch_envelope(
        world, envelope, origin="https://uncertified.example.test") is None


async def test_a_crawl_of_a_different_app_attaches_nothing(world, envelope, key):
    """The record is scoped to (tenant, app, environment). A crawl of another
    app cannot borrow it even at the same origin."""
    assert await _dispatch_envelope(world, envelope, app_id="some-other-app") is None


async def test_a_crawl_for_a_different_tenant_attaches_nothing(
        world, envelope, key):
    """Cross-tenant: tenant B's dispatch must not pick up tenant A's
    certification. (RLS enforces this in production as well; the query is
    tenant-scoped so both layers agree.)"""
    assert await _dispatch_envelope(
        world, envelope, tenant_id="tenant-beta") is None


async def test_a_prod_certification_attaches_nothing(envelope):
    """A platform admin explicitly certifying "this is production" must not
    yield a mutation proof — and the refusal cites a record rather than an
    absence."""
    prod = FakeSession()
    prod.seed(make_provisioning_record(env_kind="prod"))
    prod.seed(make_environment_row())
    await bootstrap_issuer_key(prod, envelope)
    assert await _dispatch_envelope(prod, envelope) is None


async def test_a_revoked_environment_attaches_nothing(world, envelope, key):
    """Revocation stops NEW dispatches at the source, not merely at the
    verifier — so the crawl never even carries a proof that would be refused."""
    await revocation.record_revocation(
        world, tenant_id=TENANT, subject_type=revocation.SUBJECT_ENVIRONMENT,
        subject_id=ENV, revoked_by="responder",
        cache=revocation.RevocationCache())
    assert await _dispatch_envelope(world, envelope) is None


async def test_a_moved_base_url_attaches_nothing(envelope):
    """Certify a throwaway host, then repoint ``base_url`` at production.

    At the dispatch seam the record is FOUND by origin, so this specific attack
    fails at the lookup as well as at the issuer's gate 3 — belt and braces, and
    the test states which one it is relying on: neither, because either suffices.
    """
    moved = FakeSession()
    moved.seed(make_provisioning_record())
    moved.seed(make_environment_row(base_url="https://production.example.test"))
    await bootstrap_issuer_key(moved, envelope)
    assert await _dispatch_envelope(moved, envelope) is None


async def test_no_issuer_key_attaches_nothing_and_does_not_break_the_crawl(
        world, envelope):
    """FAIL-CLOSED, NOT FAIL-STOP. With no root of trust the crawl still runs;
    it simply catalogues without persisting.

    This is the property that makes A11 safe to deploy before an operator has
    bootstrapped a key: nothing changes for anybody until they do.
    """
    keyless = FakeSession()
    keyless.seed(make_provisioning_record())
    keyless.seed(make_environment_row())
    # Exercised through _issue_walk_proof so the seeded keyless world is the
    # thing under test. Going through _walk_attestation_for_dispatch would open
    # a real database session and return None for want of a DSN — passing, but
    # proving nothing about the missing key.
    assert await _dispatch_envelope(keyless, envelope) is None


async def test_a_database_failure_attaches_nothing_and_does_not_raise(
        world, envelope, key):
    """A crawl must not fail because an OPTIONAL capability could not be
    granted. The failure degrades to a read-only crawl, loudly logged."""
    world.fail_reads_with = RuntimeError("connection reset")
    assert await _dispatch_envelope(world, envelope) is None


async def test_the_outer_wrapper_never_raises_into_the_dispatch_path(envelope):
    """The wrapper opens its own database session, so in an environment with no
    DSN it must still return None rather than raising.

    That is the same swallow-and-continue contract every other failure mode
    relies on, checked at the boundary the crawl path actually calls.
    """
    assert await explorations._walk_attestation_for_dispatch(
        envelope, tenant_id=TENANT, app_id=APP, crawl_id="c-1",
        base_url=ORIGIN, actor="dispatcher") is None


async def test_no_envelope_service_attaches_nothing(world):
    """No KMS ⇒ no unseal ⇒ no signature. There is no unsealed fallback."""
    assert await explorations._walk_attestation_for_dispatch(
        None, tenant_id=TENANT, app_id=APP, crawl_id="c-1",
        base_url=ORIGIN, actor="dispatcher") is None


async def test_ambiguous_certifications_attach_nothing(envelope):
    """Two active records at one origin cannot happen through the API (a partial
    unique index forbids it), so it means hand-edited data.

    Refusing to CHOOSE is the only safe move: any tie-break rule — "newest
    wins", "highest budget wins" — is a rule an attacker who can create a row
    gets to exploit.
    """
    ambiguous = FakeSession()
    ambiguous.seed(make_provisioning_record(environment_id="env-a"))
    ambiguous.seed(make_provisioning_record(environment_id="env-b"))
    ambiguous.seed(make_environment_row())
    await bootstrap_issuer_key(ambiguous, envelope)
    assert await _dispatch_envelope(ambiguous, envelope) is None


# ── the merge: a proof never disturbs the legacy submit tier ────────────────

def test_the_walk_envelope_never_overwrites_the_legacy_attestation():
    """The operator's signed rules-of-engagement gate the SUBMIT tier and have
    nothing to do with walk persistence. A dispatch carrying a proof must not
    disturb them."""
    legacy = {"attested_by": "ops@client.test", "env_kind": "disposable",
              "reset_procedure": "nightly reset", "expires_at_ms": 1_800_000_000_000}
    merged = explorations._merge_walk_attestation(
        legacy, {"proof": {"p": 1}, "revocations": {"r": 1}})
    for k, v in legacy.items():
        assert merged[k] == v
    assert merged["proof"] == {"p": 1}
    assert merged["revocations"] == {"r": 1}


def test_only_the_two_envelope_keys_are_merged():
    """A future issuer that returned extra keys must not be able to inject them
    into the legacy attestation, which the explorer parses with
    ``extra='forbid'`` — an unexpected key there makes the whole submit tier
    fail as ``bad_attestation``."""
    merged = explorations._merge_walk_attestation(
        {"attested_by": "ops"},
        {"proof": {}, "revocations": {}, "walk_attested": True,
         "env_kind": "disposable", "observe_only": False})
    assert set(merged) == {"attested_by", "proof", "revocations"}
    assert "walk_attested" not in merged


def test_a_proof_ships_even_without_a_legacy_attestation():
    """Walk persistence does not depend on the RoE statement. Refusing to send
    a cryptographic capability because a free-text form is missing would make
    the two mechanisms falsely interdependent."""
    merged = explorations._merge_walk_attestation(
        None, {"proof": {"p": 1}, "revocations": {"r": 1}})
    assert merged == {"proof": {"p": 1}, "revocations": {"r": 1}}


def test_no_envelope_leaves_the_legacy_attestation_exactly_as_it_was():
    """The no-op path — which is every crawl that is not walk-certified, and
    must therefore be byte-identical to pre-A11 behaviour."""
    legacy = {"attested_by": "ops", "env_kind": "staging"}
    assert explorations._merge_walk_attestation(legacy, None) is legacy
    assert explorations._merge_walk_attestation(None, None) is None


# ── the contract: qe-central cannot grant what only the verifier may grant ──

def test_qe_central_has_no_way_to_set_walk_attested():
    """THE PLATFORM CONTRACT, asserted structurally.

    ``walk_attested`` is derived on the explorer from its own verification
    verdict. If this service ever gained a way to set it, the dispatch body
    would become an authorisation channel and every cryptographic control in
    A11 would be decorative.

    Asserted over the whole qe-central source tree rather than one file: the
    risk is somebody ADDING it somewhere, and a test pinned to one module would
    not see that.
    """
    import io
    import pathlib
    import tokenize

    root = pathlib.Path(explorations.__file__).resolve().parents[1]
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "walk_attested" not in text:
            continue
        # COMMENTS AND DOCSTRINGS DO NOT COUNT. Explaining why the platform
        # must never set this flag is exactly the documentation we want; only a
        # real NAME in real code is the breach. Tokenising rather than
        # regex-matching is what keeps that distinction honest.
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
        except tokenize.TokenError:          # pragma: no cover - unparseable file
            offenders.append(f"{path.relative_to(root)} (unparseable)")
            continue
        if any(tok.type == tokenize.NAME and tok.string == "walk_attested"
               for tok in tokens):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        f"qe-central names `walk_attested` as CODE in {offenders}. Only the "
        f"explorer may set it, and only from a verification verdict — a "
        f"platform-side reference means the dispatch body has become an "
        f"authorisation channel.")


def test_the_dispatch_request_model_has_no_walk_authority_field():
    """The wire contract itself carries no field that could grant mutation.

    ``env_kind`` and ``observe_only`` ARE on the dispatch — deliberately, and
    they are not this hole: the explorer re-derives both independently and can
    only ratchet them tighter (M0.5 T-SEC-05). Walk persistence, by contrast,
    has no dispatch-settable field at all.
    """
    from app.clients.explorer_client import ExploreDispatchRequest

    fields = set(ExploreDispatchRequest.model_fields)
    assert "walk_attested" not in fields
    assert "walk_authorization" not in fields
    assert "max_walk_mutations_per_step" not in fields
    # The proof travels INSIDE `attestation`, where it is bytes to be verified
    # rather than a flag to be believed.
    assert "attestation" in fields
