"""GATE 3 / A21 — THE PRODUCER HALF: one application, three deliberate changes,
two real crawls, and a diff that has to name all three correctly.

    crawl acme-life
      →  REMOVE a question, ADD a question, CHANGE a question's answer set
      →  crawl it again
      →  write both coverage accounts to disk as evidence

WHY THIS IS NOT M2.3 AGAIN
==========================
``test_catalog_retirement_regression.py`` proves ONE classification —
``removed`` — because retirement was the milestone. A21 asks for all three, and
the three are not the same proof repeated:

  * ``removed`` and ``added`` are set differences over ``question_id``. Either
    one alone can be produced by a crawl that simply saw more or less of the
    application than the other crawl did, which is why the control group below
    matters more here than anywhere else.
  * ``changed`` is the hard one, and the only one that says anything about the
    catalogue's IDENTITY model. It requires the question to keep its
    ``question_id`` across the change while a diffed field moves underneath it.
    A change that also moves the id is reported as a removal plus an addition —
    correct, but a different statement about the application.

THE THREE CHANGES, AND WHY EACH IS THE ONE IT IS
================================================
``REMOVED — "SSN (synthetic)"``
    A plain required text input on the application form. Deliberately NOT
    "Primary beneficiary": that is M2.3's question, and two regressions cutting
    the same field out of the same file would share a failure mode. Nothing in
    the application reads ``#ssn`` (the submit handler stores ``fullName`` and
    ``beneficiary`` only), so removing the field alone leaves a working
    application rather than a broken one — a broken app is not a changed app.

``ADDED — "Occupation"``
    A new optional text input on the same form. Optional on purpose: a new
    REQUIRED field changes what the funnel has to fill before it can submit, so
    a failure to reach the review page afterwards would be ambiguous between
    "the crawl could not answer it" and "the diff is wrong".

``CHANGED — "State" gains a fourth option``
    The application starts writing business in New York. This is the change that
    tests the identity model, and the size of it is chosen from a MEASURED
    property of the signature rather than guessed:

        3 options -> option_shape "few" -> signature 60f388bc5306ec74
        4 options -> option_shape "few" -> signature 60f388bc5306ec74   (SAME)
        7 options -> option_shape "many" -> signature 90590b6cc2763894  (DIFFERENT)

    ``field_signature._option_shape`` buckets an answer set by size (<=2 binary,
    <=6 few, <=20 many, else large) so that a 12-country dropdown and a
    195-country one are recognised as the same field. Adding ONE option stays
    inside the bucket, so the question keeps its identity and the diff can say
    ``options_changed``. Adding FOUR would cross into "many", mint a new
    ``question_id``, and the same real-world change would be reported as a
    removal plus an addition. That is a real and defensible property of the
    identity model — it is recorded here because a future reader adding a fifth
    option to this fixture would otherwise turn ``changed`` into ``added`` and
    have no idea why.

WHY THE ASSERTIONS END AT THE COVERAGE ACCOUNT
==============================================
Same seam as M2.3, for the same reason: the catalogue, the fold and the diff
live in qe-central, which cannot be imported into this process (M1.7 froze that
boundary as data). This half proves what a crawl can prove — that the
application changed in three specific ways and that the crawl SAW each one — and
writes both accounts to ``Nexus_power/evidence/a21_catalog_diff/``. The consumer
half, ``platform/qe-central/tests/contract/test_a21_catalog_diff_regression.py``,
folds them into a real Postgres and asserts the diff.
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
EVIDENCE_DIR = H.SERVICE_ROOT.parent.parent / "evidence" / "a21_catalog_diff"

TARGET_APP = "acme-life"
CREDENTIALS = {"username": "qec.a21@example.test", "password": "A21!Passw0rd"}

#: The question the application STOPS asking.
REMOVED_QUESTION = "SSN (synthetic)"
#: The question the application STARTS asking.
ADDED_QUESTION = "Occupation"
#: The question that keeps its identity while its answer set grows.
CHANGED_QUESTION = "State"
#: The option that arrives on it. Compared case-insensitively downstream: the
#: page declares "NY" and the crawl's field ledger normalises option text to
#: lower case, so the two sides of the evidence spell it differently and neither
#: is wrong.
ADDED_OPTION = "NY"

#: Questions that must survive all three changes untouched — THE CONTROL GROUP.
#: Without them, "removed" could be produced by a crawl that simply saw less and
#: "added" by one that saw more; a diff over two crawls of different depth is not
#: a diff of two versions of an application. One is taken from each page the
#: funnel walks, so a crawl that lost a whole page is caught rather than averaged
#: away.
SURVIVING_QUESTIONS = (
    "Age",                                  # quote page
    "Coverage amount ($)",                  # quote page
    "Full legal name",                      # application page
    "Date of birth",                        # application page
    "Primary beneficiary",                  # application page
)

#: The exact incisions, against the committed markup. Each is (label, find,
#: replace) and each is asserted to match EXACTLY ONCE — see :func:`_change_the_application`.
_SURGERY = (
    (
        "REMOVE " + REMOVED_QUESTION,
        # The SSN field closes the two-column row it shares with Date of birth,
        # so the replacement has to keep that row closed. Dropping the whole
        # line would leave an unbalanced <div> and the page would render wrong —
        # which would change the crawl for reasons that are not this change.
        '\'<div class="field"><label for="ssn">SSN (synthetic)</label><input id="ssn" name="ssn" placeholder="900-00-0000" pattern="900-\\\\d{2}-\\\\d{4}" required></div></div>\'+',
        "'</div>'+",
    ),
    (
        "ADD " + ADDED_QUESTION,
        '\'<div class="field"><label for="beneficiary">Primary beneficiary</label><input id="beneficiary" name="beneficiary" required></div>\'+',
        '\'<div class="field"><label for="beneficiary">Primary beneficiary</label><input id="beneficiary" name="beneficiary" required></div>\'+\n'
        '        \'<div class="field"><label for="occupation">Occupation</label><input id="occupation" name="occupation"></div>\'+',
    ),
    (
        "CHANGE " + CHANGED_QUESTION,
        "<option>TX</option><option>FL</option><option>CA</option></select>",
        "<option>TX</option><option>FL</option><option>CA</option><option>NY</option></select>",
    ),
)

#: Forward-shaped label fragments the stub oracle recognises. Generic funnel
#: vocabulary, not this application's wording. Same list as M2.3 / the Golden Gate.
_FORWARD = ("quote", "continue", "next", "proceed", "apply", "review", "start", "see")

#: THE FUNNEL'S FORWARD CONTROLS, and only those. ``Bind policy`` — the app's one
#: irreversible control — is deliberately absent: reaching the application form
#: is what this regression needs, and binding a policy is not.
FUNNEL_ADVANCES = [
    {"control": "Get quote", "approved_by": "a21-catalog-diff", "max_crossings": 4},
    {"control": "Continue to application", "approved_by": "a21-catalog-diff",
     "max_crossings": 4},
    {"control": "Continue to review", "approved_by": "a21-catalog-diff",
     "max_crossings": 4},
]


async def _stub_advance_oracle(candidates: Sequence[Mapping[str, Any]],
                               page_title: str, page_url: str) -> dict[str, Any]:
    """Pick the control that advances this step — deterministically.

    Prefers a BUTTON over a LINK among the forward-shaped candidates. Not tuned
    to this application; the same stub M2.3 and the Golden Gate use, and for the
    same reason: a gate that needs a live model is a gate that fails on network
    weather.
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
    """A real, verified walk authorization — the same construction M2.3 uses.
    Without it the funnel's persistence steps cannot be actuated and neither
    crawl gets past the quote form."""
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

    Newline-NORMALISED, not raw bytes: a concurrent editor rewriting acme-life
    with CRLF endings is not a change any crawl can observe, and a guard that
    cries wolf is a guard someone deletes. Same rule on both sides of the seam
    (see the M2.3 producer, where the mismatch was found).
    """
    return hashlib.sha256(
        path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()


def _change_the_application(index_html: Path) -> dict[str, Any]:
    """Perform all three changes. Returns the stamp fields describing them.

    EVERY incision must match exactly once. A surgery that silently no-ops would
    leave the two crawls looking at the same application, and the regression
    would then 'prove' that a change nobody made was detected.
    """
    before = index_html.read_text(encoding="utf-8")
    after = before
    applied: list[str] = []
    for label, find, replace in _SURGERY:
        count = after.count(find)
        assert count == 1, (
            f"A21 surgery {label!r} did not match acme-life's markup exactly "
            f"once (found {count}). The proving ground has changed shape; update "
            f"_SURGERY to match it. Refusing to run a catalog-diff regression "
            f"whose application did not change.\n  looking for: {find[:140]!r}")
        after = after.replace(find, replace, 1)
        applied.append(label)

    # The three changes, restated as facts about the TEXT rather than as trust
    # in the replacements above.
    assert REMOVED_QUESTION in before and REMOVED_QUESTION not in after, (
        f"after surgery the application still asks {REMOVED_QUESTION!r}")
    assert ADDED_QUESTION not in before and ADDED_QUESTION in after, (
        f"{ADDED_QUESTION!r} was already in the application before the change, "
        f"so 'added' would be a statement about the crawl, not the app")
    assert f"<option>{ADDED_OPTION}</option>" not in before, (
        f"the {CHANGED_QUESTION!r} question already offered {ADDED_OPTION!r}")
    assert f"<option>{ADDED_OPTION}</option>" in after

    index_html.write_text(after, encoding="utf-8")
    return {
        "removed_question": REMOVED_QUESTION,
        "added_question": ADDED_QUESTION,
        "changed_question": CHANGED_QUESTION,
        "changed_question_added_option": ADDED_OPTION,
        "surgeries_applied": applied,
        "app_sha256_before": hashlib.sha256(before.encode("utf-8")).hexdigest(),
        "app_sha256_after": hashlib.sha256(after.encode("utf-8")).hexdigest(),
    }
    # NOTE: `before`/`after` came from read_text, so both are newline-normalised
    # and these hashes are `app_sha256` values — the number the consumer half
    # recomputes from the repository copy.


def _crawl(pw, url: str, crawl_id: str, work_dir: Path) -> dict[str, Any]:
    """ONE real crawl of ``url``; returns the coverage account it built."""
    from app.auth import AuthWindow, Credentials
    from app.crawl_constants import TRAVERSAL_FULL
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
    from tests.characterization.harness import disposable_attestation

    tenant_id = "a21-catalog-diff"
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

    # THE LANE'S OWN PAGE AND CONTEXT, and the session cleared between crawls by
    # hand — acme-life keeps its signed-in user in sessionStorage, so a
    # per-crawl fresh context dies at the login wall with auth_reason=not_persisted.
    # Measured by M2.3 before this module existed; inherited deliberately rather
    # than rediscovered.
    crawler = Crawler(
        PlaywrightBrowserPort(pw.page, pw.context),
        crawl_id=crawl_id, tenant_id=tenant_id, target_url=url,
        work_dir=str(work_dir), refuse_pack=pack,
        budget=Budget.from_dict({"max_states": 40, "max_actions": 250,
                                 "max_requests": 4000,
                                 "max_duration_ms": 420_000}),
        explorer_version=EXPLORER_VERSION, guard_version=EXPLORER_VERSION,
        refuse_pack_version=pack.version,
        config_fingerprint="a21-catalog-diff",
        guard_context=guard_ctx, identity_seed="qec-a21-catalog-diff",
        observe_only=False, traversal=TRAVERSAL_FULL,
        advance_oracle=_stub_advance_oracle,
        boundary_approvals=FUNNEL_ADVANCES,
        credentials=Credentials.from_payload(CREDENTIALS),
    )
    pw.run(crawler.run())
    return crawler._coverage.build()


def _reset_session(pw, url: str) -> None:
    """Forget everything the previous crawl signed in for.

    acme-life keeps the signed-in user AND the quote in sessionStorage, so a
    second crawl on the same page would start already authenticated and already
    quoted. It would walk a different journey from the baseline, and any
    catalogue difference between the two would be attributable to the session
    rather than to the three application changes — which is the one thing this
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


def _options_seen(coverage: Mapping[str, Any], question: str) -> list[str]:
    """The answer set the crawl read for ONE question, lower-cased.

    Prefers the state's form snapshot (the catalogue's own source) and falls
    back to the field ledger, because a control can be inventoried on a page the
    walk did not fill.
    """
    for state in (coverage.get("states") or []):
        if not isinstance(state, Mapping):
            continue
        signal = (state.get("form_snapshot_signals") or {}).get(question)
        if isinstance(signal, Mapping) and signal.get("options"):
            return sorted(str(o).strip().lower() for o in signal["options"])
    for entry in (coverage.get("field_ledger") or []):
        if isinstance(entry, Mapping) and str(entry.get("name")) == question:
            return sorted(str(o).strip().lower() for o in (entry.get("options") or []))
    return []


def _substantive_fingerprint(coverage: Mapping[str, Any]) -> dict[str, Any]:
    """What a re-run of this producer MUST reproduce, with the volatile parts
    left out.

    THE COVERAGE ACCOUNTS ARE NOT BYTE-REPRODUCIBLE, and pretending otherwise
    produces a guard that can only fail. ``FixtureServer`` binds an EPHEMERAL
    port, so every URL in a fresh recording differs from the committed one, and
    every ``*_ms`` field is a wall clock. Measured on the M2.3 recording: a
    re-run of an unchanged producer against an unchanged application rewrote 117
    of its lines — all of them ports and timings, not one of them a fact about
    the application.

    So the artifact that CI compares is this projection instead: the questions
    each crawl asked, their answer sets and their types. That is the whole of
    what the consumer half reads, it is stable across runs, and it still changes
    the moment the proving ground or the crawler starts producing different
    coverage — which is the only thing the guard was ever meant to catch.
    """
    questions: dict[str, Any] = {}
    for state in (coverage.get("states") or []):
        if not isinstance(state, Mapping):
            continue
        for name, signal in (state.get("form_snapshot_signals") or {}).items():
            if not isinstance(signal, Mapping):
                continue
            entry = questions.setdefault(str(name), {})
            entry["type"] = str(signal.get("type") or "")
            entry["required"] = bool(signal.get("required"))
            entry["options"] = sorted(
                str(o).strip().lower() for o in (signal.get("options") or []))
    return {
        "questions": {k: questions[k] for k in sorted(questions)},
        "question_count": len(questions),
    }


@pytest.fixture(scope="module")
def diff_evidence(pw, tmp_path_factory) -> dict[str, Any]:
    """Crawl → change the application three ways → crawl again. Both accounts,
    on disk."""
    if not (PROVING_GROUNDS / TARGET_APP / "index.html").is_file():
        pytest.skip(f"{TARGET_APP} not found under {PROVING_GROUNDS}")

    # Serve a COPY: the repository's proving ground must not be left mutated by a
    # test run, but the application the second crawl meets must genuinely be a
    # different application.
    tree = tmp_path_factory.mktemp("a21_grounds")
    shutil.copytree(PROVING_GROUNDS / TARGET_APP, tree / TARGET_APP)
    index_html = tree / TARGET_APP / "index.html"
    out = H.HERE / "_crawl_out" / "a21_catalog_diff"

    server = H.FixtureServer(root=tree).start()
    try:
        url = server.url(TARGET_APP)
        _reset_session(pw, url)
        before = _crawl(pw, url, "a21-baseline", out / "baseline")
        stamp_fields = _change_the_application(index_html)
        _reset_session(pw, url)
        after = _crawl(pw, url, "a21-after-change", out / "after")
    finally:
        server.stop()

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = {
        "milestone": "GATE3-A21",
        "app": TARGET_APP,
        "surviving_questions": list(SURVIVING_QUESTIONS),
        "baseline_crawl": "a21-baseline",
        "after_crawl": "a21-after-change",
        # THE GUARDABLE PROJECTION — see :func:`_substantive_fingerprint`. The
        # coverage accounts beside this file are the full evidence and are NOT
        # byte-reproducible; this is.
        "fingerprint": {
            "baseline": _substantive_fingerprint(before),
            "after": _substantive_fingerprint(after),
        },
        **stamp_fields,
    }
    (EVIDENCE_DIR / "stamp.json").write_text(
        json.dumps(stamp, indent=2, sort_keys=True), encoding="utf-8")
    for name, cov in (("coverage_baseline.json", before),
                      ("coverage_after_change.json", after)):
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


# ── THE BASELINE HAS TO HAVE SEEN WHAT THE CHANGE IS ABOUT ───────────────────

def test_the_baseline_crawl_reached_all_three_questions(diff_evidence) -> None:
    """Nothing downstream means anything unless the baseline saw the ground the
    change moves. A ``removed`` derived from a question the first crawl never
    reached is a statement about crawl depth, not about the application."""
    cov = diff_evidence["before"]
    seen = _questions_seen(cov)
    for question in (REMOVED_QUESTION, CHANGED_QUESTION):
        assert question in seen, (
            f"the baseline crawl never reached {question!r}, so a later "
            f"difference would prove nothing about the application."
            + _diagnose(cov, "baseline"))
    assert ADDED_QUESTION not in seen, (
        f"the baseline crawl already saw {ADDED_QUESTION!r} — it is supposed to "
        f"arrive with the change." + _diagnose(cov, "baseline"))


# ── THE THREE CHANGES, EACH OBSERVED ─────────────────────────────────────────

def test_the_second_crawl_no_longer_sees_the_removed_question(diff_evidence) -> None:
    cov = diff_evidence["after"]
    assert REMOVED_QUESTION not in _questions_seen(cov), (
        f"{REMOVED_QUESTION!r} was deleted from the application and the crawl "
        f"still reports it." + _diagnose(cov, "after"))


def test_the_second_crawl_sees_the_added_question(diff_evidence) -> None:
    cov = diff_evidence["after"]
    assert ADDED_QUESTION in _questions_seen(cov), (
        f"{ADDED_QUESTION!r} was added to the application and the crawl did not "
        f"report it. An 'added' the crawl cannot see is an 'added' the diff "
        f"cannot report." + _diagnose(cov, "after"))


def test_the_changed_question_gained_its_option_and_kept_its_name(
    diff_evidence,
) -> None:
    """The CHANGE, observed as a change rather than as a replacement.

    The question must still be asked under the same wording — if the label moved
    too, the catalogue would mint a new id and the diff would correctly report a
    removal plus an addition, which is a different finding from the one A21 asks
    for.
    """
    before_opts = _options_seen(diff_evidence["before"], CHANGED_QUESTION)
    after_opts = _options_seen(diff_evidence["after"], CHANGED_QUESTION)
    assert CHANGED_QUESTION in _questions_seen(diff_evidence["after"]), (
        f"{CHANGED_QUESTION!r} is no longer asked at all; this regression needs "
        f"it CHANGED, not removed." + _diagnose(diff_evidence["after"], "after"))
    assert before_opts, (
        f"the baseline read no options for {CHANGED_QUESTION!r}, so 'the answer "
        f"set grew' cannot be established")
    added = set(after_opts) - set(before_opts)
    assert added == {ADDED_OPTION.lower()}, (
        f"{CHANGED_QUESTION!r} options went {before_opts} -> {after_opts}; "
        f"expected exactly {ADDED_OPTION.lower()!r} to arrive and nothing else "
        f"to move.")


def test_the_surviving_questions_are_still_asked(diff_evidence) -> None:
    """THE CONTROL GROUP. A crawl that simply saw less would also 'lose' the
    removed question, and a crawl that saw more would 'gain' the added one; the
    classifications would then be artefacts of crawl depth rather than facts
    about the application."""
    seen = _questions_seen(diff_evidence["after"])
    missing = [q for q in SURVIVING_QUESTIONS if q not in seen]
    assert not missing, (
        f"questions that were NOT changed are missing from the second crawl: "
        f"{missing}. The second crawl saw less of the application, so its "
        f"catalogue difference cannot be attributed to the three changes."
        + _diagnose(diff_evidence["after"], "after"))


def test_exactly_three_questions_moved(diff_evidence) -> None:
    """The whole finding in one assertion, at the level a crawl can state it.

    Anything else appearing or disappearing between the two crawls makes every
    classification downstream ambiguous — the consumer half would be diffing two
    different walks, not two versions of one application.
    """
    before = _questions_seen(diff_evidence["before"])
    after = _questions_seen(diff_evidence["after"])
    assert before - after == {REMOVED_QUESTION}, (
        f"questions that vanished between the crawls: {sorted(before - after)}; "
        f"expected only {REMOVED_QUESTION!r}")
    assert after - before == {ADDED_QUESTION}, (
        f"questions that appeared between the crawls: {sorted(after - before)}; "
        f"expected only {ADDED_QUESTION!r}")


def test_both_crawls_are_conclusive_enough_to_diff_on(diff_evidence) -> None:
    """The consumer half refuses to retire a question on a crawl that ran
    degraded (``catalog.crawl_evidence``). If either crawl here were degraded,
    this regression would be measuring that rule rather than the three changes.

    ``auth_incomplete`` is deliberately NOT among the conditions, and that is
    measured rather than assumed: acme-life keeps its signed-in user in
    sessionStorage, so both crawls report it with reason ``not_persisted`` — the
    login verified, the app dropped it on the next page load, and the crawler
    recovered in place. That flag describes coverage BREADTH, not the
    trustworthiness of what was read. ``auth_blocked`` — never got in at all — is
    asserted.
    """
    for which in ("before", "after"):
        cov = diff_evidence[which]
        assert cov.get("states"), f"[{which}] observed no states"
        assert cov.get("flows"), (
            f"[{which}] recorded states but walked no journey — entry snapshots, "
            f"not an observation of the application")
        assert not cov.get("auth_blocked"), f"[{which}] auth blocked"
        assert int(cov.get("inventory_failures") or 0) == 0, (
            f"[{which}] {cov.get('inventory_failures')} page(s) would not read: "
            f"{cov.get('inventory_failure_detail')}")


def test_the_evidence_is_written_for_the_consumer_half(diff_evidence) -> None:
    """The artifact IS the service boundary; if it is not on disk, qe-central's
    half has nothing real to consume and would fall back to a fixture."""
    for name in ("stamp.json", "coverage_baseline.json",
                 "coverage_after_change.json"):
        path = EVIDENCE_DIR / name
        assert path.is_file() and path.stat().st_size > 0, f"{path} was not written"
    stamp = json.loads((EVIDENCE_DIR / "stamp.json").read_text(encoding="utf-8"))
    live = app_sha256(PROVING_GROUNDS / TARGET_APP / "index.html")
    assert stamp["app_sha256_before"] == live, (
        "the baseline was crawled against a different acme-life than the one in "
        "the repository right now — re-run this producer rather than trusting a "
        "recording of an application that has since changed")
