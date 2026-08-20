"""M2.3 / T-ST-04 — THE PRODUCER HALF: a first-party application really loses a
question, and two real crawls record the difference.

WHAT THIS DOES, IN ORDER
========================
    crawl acme-life  →  DELETE a question from acme-life  →  crawl it again
    →  write both coverage accounts to disk as evidence

Nothing is mocked but the tier-3 advance oracle (the same deterministic stub the
Golden Gate uses, and for the same reason: a gate that needs a live model is a
gate that fails on network weather). The crawls run through the PRODUCTION
:class:`app.crawler.Crawler` and the PRODUCTION
:class:`app.main.PlaywrightBrowserPort`, in real Chromium, against the real
``proving-grounds/acme-life`` markup.

THE APPLICATION CHANGE IS REAL
==============================
Between the two crawls this module DELETES the "Primary beneficiary" question
from the application's source — the field, the handler that reads it, and the
row that displays it back — by exact-string surgery on the served copy of the
committed ``index.html``. Every incision is asserted to have matched. If someone
edits acme-life such that an anchor no longer appears, this goes RED and says
which one, rather than quietly performing no surgery and "proving" that a
question nobody removed was not removed.

The surgery is done on a COPY served from a temp tree, so the repository's
proving ground is not left mutated by a test run. The application the second
crawl meets is nevertheless a genuinely different application from the one the
first crawl met — it does not ask that question any more.

WHY THE ASSERTIONS END AT THE COVERAGE ACCOUNT
==============================================
The catalogue, the fold and the retirement live in qe-central, a different
service with a different ``app`` package; the two cannot be imported into one
process (M1.7 established this and froze the boundary as data). So this half
proves what a crawl can prove — that the application stopped asking, and that
the crawl SAW it stop — and writes both coverage accounts, byte for byte as the
crawler built them, to ``Nexus_power/evidence/m23_retirement/``.

The consumer half — fold → catalogue → retirement → diff, against a real
Postgres — reads those two accounts in
``platform/qe-central/tests/contract/test_m23_retirement_regression.py``. It
verifies the ``app_sha256`` stamps this module writes against the CURRENT
acme-life source, so a stale recording cannot be mistaken for a fresh one: edit
the proving ground and the consumer half goes red until these crawls are re-run.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright, pytest.mark.proving_ground]

PROVING_GROUNDS = H.SERVICE_ROOT.parent.parent / "proving-grounds"

#: Where the two coverage accounts land. Shared with qe-central, which cannot
#: import this service — the artifact IS the seam.
EVIDENCE_DIR = H.SERVICE_ROOT.parent.parent / "evidence" / "m23_retirement"

TARGET_APP = "acme-life"
CREDENTIALS = {"username": "qec.m23@example.test", "password": "M23!Passw0rd"}

#: The question the application stops asking. A plain text input on the
#: application form: it reaches the catalogue as a NODE CONTROL (through
#: ``form_snapshot_signals``), which is the path every text, date and number
#: question in every application takes.
REMOVED_QUESTION = "Primary beneficiary"

#: Questions that must survive the change untouched — the control group. Without
#: them "removed" could be produced by a crawl that simply saw less, and a diff
#: that empties the catalogue is not a diff that detected a removal.
SURVIVING_QUESTIONS = ("Full legal name", "Date of birth", "SSN (synthetic)")

#: The exact incisions, against the committed markup. Each is (find, replace).
#: Asserted to match — see :func:`_remove_the_question`.
_SURGERY = (
    # 1. The question itself.
    ("""'<div class="field"><label for="beneficiary">Primary beneficiary</label><input id="beneficiary" name="beneficiary" required></div>'+\n""",
     ""),
    # 2. The handler that read it. Left in place it would throw on a null
    #    element and the application would stop working — a broken app is not a
    #    changed app, and the crawl would be measuring the wrong thing.
    ("""state.applicant={ fullName:document.getElementById("fullName").value, beneficiary:document.getElementById("beneficiary").value };""",
     """state.applicant={ fullName:document.getElementById("fullName").value };"""),
    # 3. The row that displayed the answer back on the review page.
    ("""'<div class="field"><label>Beneficiary</label><div>'+esc(state.applicant.beneficiary)+'</div></div>'+\n""",
     ""),
)

#: Forward-shaped label fragments the stub oracle recognises. Generic funnel
#: vocabulary, not this application's wording. Same list as the Golden Gate.
_FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start", "see")

#: THE FUNNEL'S FORWARD CONTROLS, and only those.
#:
#: ``Crawler._submit_enabled`` is False unless the crawl carries at least one
#: approval, and a walk that cannot submit stops at the first form it meets — the
#: quote page. Measured: with no grants at all both crawls ended at
#: ``deepest_flow_steps=1, terminal=submit_boundary`` and never rendered the
#: application form, so neither crawl could see the question this regression is
#: about removing.
#:
#: These are the three step-advancing submits of the funnel, named one at a time.
#: ``Bind policy`` — the app's one irreversible control — is DELIBERATELY absent:
#: reaching the application form is what this regression needs, and binding a
#: policy is not. Crossing it is the Golden Gate's job, under its own grant.
FUNNEL_ADVANCES = [
    {"control": "Get quote", "approved_by": "m23-retirement", "max_crossings": 4},
    {"control": "Continue to application", "approved_by": "m23-retirement",
     "max_crossings": 4},
    {"control": "Continue to review", "approved_by": "m23-retirement",
     "max_crossings": 4},
]


async def _stub_advance_oracle(candidates: Sequence[Mapping[str, Any]],
                               page_title: str, page_url: str) -> dict[str, Any]:
    """Pick the control that advances this step — deterministically.

    Prefers a BUTTON over a LINK among the forward-shaped candidates, which is
    the same reasoning a model applies and the same rule the Golden Gate's stub
    uses. Not tuned to this application.
    """
    forward = [c for c in candidates
               if any(f in str(c.get("name") or "").lower() for f in _FORWARD)]
    if not forward:
        return {}
    buttons = [c for c in forward if str(c.get("kind") or "").lower() == "button"]
    pick = (buttons or forward)[0]
    return {"name": pick.get("name"), "kind": pick.get("kind"),
            "reason": "deterministic stub: forward-shaped, button preferred"}


def _walk_authorization(crawl_id: str, tenant_id: str, target_url: str) -> Any:
    """A real, verified walk authorization — the same construction the Golden
    Gate uses. Without it the funnel's persistence steps cannot be actuated and
    neither crawl gets past the quote form."""
    from app.attest import ProofReplayGuard, verify_provisioning_proof
    from app.walk_persist import MutationAuditLog, WalkAuthorization
    from _attest_kit import Issuer

    issuer = Issuer()
    scheme, _, rest = target_url.partition("//")
    origin = f"{scheme}//{rest.split('/')[0]}"
    verdict = verify_provisioning_proof(
        {"proof": issuer.proof(crawl_id=crawl_id, tenant_id=tenant_id,
                               target_origin=origin,
                               max_walk_mutations_per_step=8),
         "revocations": issuer.revocations()},
        trust=issuer.trust(), crawl_id=crawl_id, tenant_id=tenant_id,
        target_url=target_url, replay_guard=ProofReplayGuard())
    assert verdict.authorized, (
        f"could not build a walk authorization: {verdict.reason}")
    return WalkAuthorization.from_verdict(
        verdict, workflow_id=crawl_id, audit=MutationAuditLog())


def app_sha256(path: Path) -> str:
    """The application source's identity, ignoring line-ending churn.

    Hashes the text with newlines NORMALISED rather than the raw bytes. The two
    are not the same number on Windows and the difference is not about the
    application: a concurrent editor rewrote acme-life with CRLF endings, the
    producer hashed newline-normalised text while the consumer's guard hashed
    raw bytes, and the guard fired on a file whose content had not changed in any
    way a crawl could see. A guard that cries wolf is a guard someone deletes, so
    both sides now ask the same question — has the SOURCE changed — and a
    line-ending rewrite correctly answers no.
    """
    return hashlib.sha256(
        path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _remove_the_question(index_html: Path) -> tuple[str, str]:
    """Perform the application change. Returns (sha before, sha after).

    Every incision must match. A surgery that silently no-ops would leave the
    two crawls looking at the same application, and the regression would then
    'prove' that nothing was removed by removing nothing.
    """
    before = index_html.read_text(encoding="utf-8")
    after = before
    for i, (find, replace) in enumerate(_SURGERY, start=1):
        assert after.count(find) == 1, (
            f"M2.3 surgery {i}/{len(_SURGERY)} did not match acme-life's markup "
            f"exactly once (found {after.count(find)}). The proving ground has "
            f"changed shape; update _SURGERY to match it. Refusing to run a "
            f"removal regression that removes nothing.\n  looking for: {find[:120]!r}")
        after = after.replace(find, replace, 1)
    assert REMOVED_QUESTION in before and REMOVED_QUESTION not in after, (
        f"after surgery the application still contains {REMOVED_QUESTION!r}")
    index_html.write_text(after, encoding="utf-8")
    return (hashlib.sha256(before.encode("utf-8")).hexdigest(),
            hashlib.sha256(after.encode("utf-8")).hexdigest())
    # NOTE: `before`/`after` came from read_text, so both are newline-normalised
    # and these hashes are `app_sha256` values — the same number the consumer
    # half computes from the repository copy.


def _crawl(pw, url: str, crawl_id: str, work_dir: Path) -> dict[str, Any]:
    """ONE real crawl of ``url``; returns the coverage account it built."""
    from app.auth import AuthWindow, Credentials
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
    from tests.characterization.harness import disposable_attestation

    tenant_id = "m23-retirement"
    pack = load_refuse_pack(str(H.SERVICE_ROOT / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=400, window_ms=240_000),
        attestation=disposable_attestation(),
        submit_flow_approved=True,
        walk_authorization=_walk_authorization(crawl_id, tenant_id, url),
        idp_domains=frozenset(),
    )
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    # THE LANE'S OWN PAGE AND CONTEXT — the Golden Gate's configuration exactly.
    # A per-crawl fresh context was tried first and both crawls died at the login
    # wall with ``auth_reason=not_persisted`` and zero flows: acme-life keeps its
    # signed-in user in sessionStorage, and the authenticator's recovery from a
    # session that does not survive a page load is exercised on the lane's page.
    # Since the two configurations are not equivalent for this application, this
    # module uses the one a real crawl is known to complete on, and clears the
    # session between crawls itself (see ``_reset_session``) rather than relying
    # on context isolation it cannot have.
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id=tenant_id, target_url=url,
        work_dir=str(work_dir), refuse_pack=pack,
        budget=Budget.from_dict({"max_states": 40, "max_actions": 250,
                                 "max_requests": 4000,
                                 "max_duration_ms": 420_000}),
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint="m23-retirement",
        guard_context=guard_ctx, identity_seed="qec-m23-retirement",
        observe_only=False, traversal=TRAVERSAL_FULL,
        advance_oracle=_stub_advance_oracle,
        boundary_approvals=FUNNEL_ADVANCES,
        credentials=Credentials.from_payload(CREDENTIALS),
    )
    pw.run(crawler.run())
    return crawler._coverage.build()


def _reset_session(pw, url: str) -> None:
    """Forget everything the previous crawl signed in for.

    acme-life keeps the signed-in user AND the quote in ``sessionStorage``, so a
    second crawl on the same page would start already authenticated and already
    quoted. It would then walk a different journey from the baseline, and any
    catalogue difference between the two would be attributable to the session
    rather than to the application change — which is the one thing this
    regression exists to rule out.
    """
    async def _clear():
        await pw.context.clear_cookies()
        await pw.page.goto(url)
        await pw.page.evaluate(
            "() => { try { sessionStorage.clear(); localStorage.clear(); } "
            "catch (e) {} }")
        await pw.page.goto("about:blank")
    pw.run(_clear())


def _questions_seen(coverage: Mapping[str, Any]) -> set[str]:
    """Every question label this crawl's states actually asked.

    Read from ``coverage.states[*].form_snapshot_signals`` — the producer side of
    the contract qe-central's catalogue is built from — so what this module
    asserts about and what the catalogue is fed are the same evidence.
    """
    seen: set[str] = set()
    for state in (coverage.get("states") or []):
        if isinstance(state, Mapping):
            seen |= {str(k) for k in (state.get("form_snapshot_signals") or {})}
    return seen


@pytest.fixture(scope="module")
def retirement_evidence(pw, tmp_path_factory) -> dict[str, Any]:
    """Crawl → change the application → crawl again. Both accounts, on disk."""
    if not (PROVING_GROUNDS / TARGET_APP / "index.html").is_file():
        pytest.skip(f"{TARGET_APP} not found under {PROVING_GROUNDS}")

    # Serve a COPY: the repository's proving ground must not be left mutated by
    # a test run, but the application the second crawl meets must genuinely be a
    # different application.
    tree = tmp_path_factory.mktemp("m23_grounds")
    shutil.copytree(PROVING_GROUNDS / TARGET_APP, tree / TARGET_APP)
    index_html = tree / TARGET_APP / "index.html"
    out = H.HERE / "_crawl_out" / "m23_retirement"

    server = H.FixtureServer(root=tree).start()
    try:
        url = server.url(TARGET_APP)
        _reset_session(pw, url)
        before = _crawl(pw, url, "m23-baseline", out / "baseline")
        sha_before, sha_after = _remove_the_question(index_html)
        _reset_session(pw, url)
        after = _crawl(pw, url, "m23-after-removal", out / "after")
    finally:
        server.stop()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = {
        "milestone": "M2.3",
        "app": TARGET_APP,
        "removed_question": REMOVED_QUESTION,
        "surviving_questions": list(SURVIVING_QUESTIONS),
        # THE ANTI-FOSSIL GUARD. The consumer half re-hashes the CURRENT
        # acme-life source and compares: a recording made against a proving
        # ground that has since changed is not evidence about the proving ground
        # that ships, and must be re-run rather than trusted.
        "app_sha256_before": sha_before,
        "app_sha256_after": sha_after,
        "baseline_crawl": "m23-baseline",
        "after_crawl": "m23-after-removal",
    }
    (EVIDENCE_DIR / "stamp.json").write_text(
        json.dumps(stamp, indent=2, sort_keys=True), encoding="utf-8")
    for name, cov in (("coverage_baseline.json", before),
                      ("coverage_after_removal.json", after)):
        (EVIDENCE_DIR / name).write_text(
            json.dumps(cov, indent=2, sort_keys=True, default=str),
            encoding="utf-8")
    return {"before": before, "after": after, "stamp": stamp}


def _diagnose(cov: Mapping[str, Any], which: str) -> str:
    return (f"\n  [{which}] states        : {len(cov.get('states') or [])}"
            f"\n  [{which}] questions     : {sorted(_questions_seen(cov))}"
            f"\n  [{which}] auth_blocked  : {cov.get('auth_blocked')}"
            f"\n  [{which}] auth_incomplete: {cov.get('auth_incomplete')}"
            f"\n  [{which}] inv_failures  : {cov.get('inventory_failures')}")


def test_the_baseline_crawl_reached_the_question(retirement_evidence) -> None:
    """Nothing downstream means anything unless the baseline SAW the question."""
    cov = retirement_evidence["before"]
    seen = _questions_seen(cov)
    assert REMOVED_QUESTION in seen, (
        f"the baseline crawl never reached {REMOVED_QUESTION!r}, so a later "
        f"absence would prove nothing about the application — only about how far "
        f"the crawl got." + _diagnose(cov, "baseline"))


def test_the_second_crawl_no_longer_sees_the_removed_question(
    retirement_evidence,
) -> None:
    """The application changed, and the crawl noticed."""
    cov = retirement_evidence["after"]
    assert REMOVED_QUESTION not in _questions_seen(cov), (
        f"{REMOVED_QUESTION!r} was deleted from the application and the crawl "
        f"still reports it." + _diagnose(cov, "after"))


def test_the_surviving_questions_are_still_asked(retirement_evidence) -> None:
    """THE CONTROL GROUP. A crawl that simply saw less would also 'lose' the
    removed question, and the retirement would be an artefact of a short crawl
    rather than a fact about the application."""
    seen = _questions_seen(retirement_evidence["after"])
    missing = [q for q in SURVIVING_QUESTIONS if q not in seen]
    assert not missing, (
        f"questions that were NOT removed are missing from the second crawl: "
        f"{missing}. The second crawl saw less of the application, so its "
        f"catalogue difference cannot be attributed to the removal."
        + _diagnose(retirement_evidence["after"], "after"))


def test_both_crawls_are_conclusive_enough_to_retire_on(retirement_evidence) -> None:
    """Retirement is gated on the crawl's OWN self-report of completeness (see
    ``catalog.crawl_evidence``). If either crawl ran degraded, the consumer half
    would correctly refuse to retire, and this regression would then be measuring
    the evidence rule rather than the removal.

    The conditions asserted here are exactly the ones that rule reads, kept in
    step with it deliberately: this is the producer-side statement of what a
    retirable crawl looks like.

    ``auth_incomplete`` is NOT among them, and that is measured rather than
    assumed. acme-life keeps its signed-in user in ``sessionStorage``, so both
    crawls report ``auth_incomplete`` with reason ``not_persisted``: the login
    verified, the application dropped it on the next page load, and the crawler
    recovered by continuing in place — then walked the funnel and read the
    application form. That flag describes coverage BREADTH, not the
    trustworthiness of what was read, and breadth is handled one page at a time
    on the consumer side. ``auth_blocked`` — never got in at all — is asserted.
    """
    for which in ("before", "after"):
        cov = retirement_evidence[which]
        assert cov.get("states"), f"[{which}] observed no states"
        assert cov.get("flows"), (
            f"[{which}] recorded states but walked no journey — entry snapshots, "
            f"not an observation of the application")
        assert not cov.get("auth_blocked"), f"[{which}] auth blocked"
        assert int(cov.get("inventory_failures") or 0) == 0, (
            f"[{which}] {cov.get('inventory_failures')} page(s) would not read: "
            f"{cov.get('inventory_failure_detail')}")
        summary = cov.get("flow_summary") or {}
        assert "advances_by_tier" in summary, (
            f"[{which}] no tier rollup — the account reads as pre-hardening")


def test_the_evidence_is_written_for_the_consumer_half(retirement_evidence) -> None:
    """The artifact IS the service boundary; if it is not on disk, qe-central's
    half has nothing real to consume and would fall back to a fixture."""
    for name in ("stamp.json", "coverage_baseline.json",
                 "coverage_after_removal.json"):
        path = EVIDENCE_DIR / name
        assert path.is_file() and path.stat().st_size > 0, f"{path} was not written"
    stamp = json.loads((EVIDENCE_DIR / "stamp.json").read_text(encoding="utf-8"))
    live = app_sha256(PROVING_GROUNDS / TARGET_APP / "index.html")
    assert stamp["app_sha256_before"] == live, (
        "the baseline was crawled against a different acme-life than the one in "
        "the repository right now")
