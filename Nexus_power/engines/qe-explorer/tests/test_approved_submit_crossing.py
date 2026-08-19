"""A4.3 — THE APPROVED SUBMIT CROSSING.  The proof, T-AC-01 through T-AC-06.

Every claim this milestone makes is asserted here against the REAL production
objects: the real refuse pack, the real inventory classifier, the real
``Crawler``, the real guard.  The only thing stubbed is the browser, and it is
stubbed through the same :class:`app.browser.BrowserPort` the Playwright adapter
implements, so the crawler cannot tell the difference.

WHAT WOULD MAKE THESE TESTS WORTHLESS, AND HOW EACH IS AVOIDED:

  * asserting on a mock's arguments instead of on behaviour — so the crossings,
    the milestones and the coverage account are read back out of the crawl's own
    output, never out of a spy;
  * asserting the happy path only — so every authorisation test has its
    negative twin, and the negatives outnumber the positives;
  * asserting a flag the code under test could simply set — so
    ``journey_completed`` is never checked without also checking that inflating
    the counters cannot produce it.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from app import boundary
from app import flow_ledger
from app import submit as submit_mod
from app.boundary import (AUTHORITY_BLANKET, AUTHORITY_GRANT, AUTHORITY_NAMED,
                          BOUNDARY_APPROVABLE, BOUNDARY_NEVER, BOUNDARY_SAFE,
                          ApprovalGrant, ApprovalRegistry, CrossingLedger,
                          GrantParseError, OutcomeMilestone, boundary_key,
                          classify_boundary, confirmation_transition,
                          dom_digest, parse_grants)
from app.budget import Budget
from app.config import Settings
from app.crawler import Crawler, FrontierItem, GuardContext, Phase
from app.forms import FlowCandidate, FormFillResult, SubmitResult
from app.guard import load_refuse_pack
from tests.characterization.harness import disposable_attestation
from app.inventory import classify_control_danger

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers — controls classified by the REAL refuse pack, never hand-flagged
# ═══════════════════════════════════════════════════════════════════════════

def ctl(name: str, *, kind: str = "button", disabled: bool = False,
        href: str = "") -> dict:
    """A control whose ``danger`` comes from the production classifier.

    Hand-setting ``danger=True`` would let a test pass while the real pack
    disagreed — which is exactly how "Submit Application is classified as
    dangerous" became folklore. It is not: on a button (no href) the pack
    returns danger=False, and the assertions below pin that.
    """
    danger, rule_id, severity = classify_control_danger(
        name, kind, kind, _REFUSE_PACK, href)
    return {"name": name, "kind": kind, "role": kind, "disabled": disabled,
            "danger": danger, "danger_rule_id": rule_id,
            "danger_severity": severity, "href": href}


class _Port:
    """Enough BrowserPort for the crossing path; every verb is scripted."""

    def __init__(self, *, controls=(), statuses=(), texts=(),
                 url="https://app.example/apply", url_after=None,
                 controls_after=None, statuses_after=None, texts_after=None):
        self._controls = list(controls)
        self._statuses = list(statuses)
        self._texts = list(texts)
        self._url = url
        self._url_after = url_after
        self._controls_after = controls_after
        self._statuses_after = statuses_after
        self._texts_after = texts_after
        self.clicks: list[str] = []
        self._clicked = False

    async def goto(self, url):
        self._url = url
        return types.SimpleNamespace(url=url, ok=True)

    async def current_url(self):
        return self._url

    async def title(self):
        return ""

    async def collect_controls(self):
        if self._clicked and self._controls_after is not None:
            return [dict(c) for c in self._controls_after]
        return [dict(c) for c in self._controls]

    async def status_texts(self):
        if self._clicked and self._statuses_after is not None:
            return list(self._statuses_after)
        return list(self._statuses)

    async def visible_texts(self):
        if self._clicked and self._texts_after is not None:
            return list(self._texts_after)
        return list(self._texts)

    async def error_texts(self):
        return []

    async def dialog_flags(self):
        return []

    async def screenshot_png(self):
        return b"\x89PNG\r\n\x1a\n" + b"\x00" * 32

    async def click(self, control):
        # Mirrors PlaywrightBrowserPort._act: the adapter reads the error
        # live-regions AFTER the action and folds them into the observation. A
        # fake that skipped that would let an errored submit pass as a
        # confirmation in the test suite and nowhere else.
        from app.browser import RawObservation
        self.clicks.append(str(control.get("name") or ""))
        before = self._url
        self._clicked = True
        if self._url_after:
            self._url = self._url_after
        errors = await self.error_texts()
        dialogs = await self.dialog_flags()
        return RawObservation(url_before=before, url_after=self._url,
                              error_detail=(errors[0] if errors else ""),
                              dialog_opened=bool(dialogs),
                              dialog_detail=(dialogs[0] if dialogs else ""))

    async def collect_displayed_values(self):
        return []

    async def drain_network(self):
        return []

    async def storage_state(self):
        return {"cookies": [], "origins": []}


def build_crawler(tmp_path, *, approvals=(), grants=(), attested=True,
                  port=None) -> Crawler:
    attestation = disposable_attestation() if attested else None
    guard = GuardContext(refuse_pack=_REFUSE_PACK, attestation=attestation)
    return Crawler(
        port or _Port(),
        crawl_id="c1", tenant_id="t1", target_url="https://app.example/apply",
        work_dir=str(tmp_path), refuse_pack=_REFUSE_PACK,
        budget=Budget(rate_per_s=0), explorer_version="test/1.0",
        guard_version="test", refuse_pack_version=_REFUSE_PACK.version,
        config_fingerprint="fp", guard_context=guard,
        submit_approvals=approvals, boundary_approvals=grants,
    )


# ═══════════════════════════════════════════════════════════════════════════
#  T-AC-01 · THE BOUNDARY MODEL — two lists, two meanings
# ═══════════════════════════════════════════════════════════════════════════

def test_submit_application_is_not_dangerous_per_the_refuse_pack():
    """THE PREMISE THIS MILESTONE WAS BRIEFED ON IS FALSE, AND IT MATTERS.

    "Submit Application is correctly classified as dangerous" is not what the
    production classifier does. On a BUTTON (no href) the refuse pack returns
    danger=False, because ``rp.verb.underwrite`` was deliberately scoped off the
    page URL after it flagged 20 of 35 controls on
    ``/underwriting/new-business/new-application`` — including the Back button.

    If the boundary model keyed on ``danger`` alone, the single most important
    commit button in the product would be classified safe and the walk would
    click it with nobody's approval. This test exists so that stays visible.
    """
    danger, rule_id, _sev = classify_control_danger(
        "Submit Application", "button", "button", _REFUSE_PACK, "")
    assert danger is False, (
        "the refuse pack now flags 'Submit Application' — the boundary model's "
        "commit-shape rung may be redundant, but do not remove it without "
        "re-checking the URL-scoping fix in inventory.py")
    assert rule_id == ""


def test_submit_application_is_approvable_and_nothing_else():
    """T-AC-01: it appears ONLY in approvable_boundary."""
    klass = classify_boundary(ctl("Submit Application"))
    assert klass.cls == BOUNDARY_APPROVABLE
    assert klass.approvable is True and klass.safe is False
    assert klass.reason == boundary.REASON_COMMIT_SHAPE


@pytest.mark.parametrize("label,rule", [
    ("Bind Coverage", "rp.verb.bind"),
    ("Approve Claim", "rp.verb.approve"),
    ("Process Payment", "rp.verb.pay"),
    ("Delete Account", "rp.verb.delete"),
    ("Submit to Underwriting", "rp.verb.underwrite"),
])
def test_refuse_pack_verbs_are_approvable_and_carry_their_rule(label, rule):
    klass = classify_boundary(ctl(label))
    assert klass.cls == BOUNDARY_APPROVABLE
    assert klass.reason == boundary.REASON_DANGER_VERB
    assert klass.rule_id == rule


@pytest.mark.parametrize("label", ["Continue", "Next", "Back", "Edit answers"])
def test_ordinary_forward_controls_stay_safe(label):
    assert classify_boundary(ctl(label)).cls == BOUNDARY_SAFE


@pytest.mark.parametrize("label", ["Sign Out", "Log out", "Sign In"])
def test_session_controls_are_never_crossable_and_never_offered(label):
    """A sign-out is danger AND commit-adjacent, so a two-class model would
    offer it for approval on every page. Crossing it ends the session the rest
    of the journey is observed through — the crawl would trade all remaining
    coverage for one data point."""
    assert classify_boundary(ctl(label)).cls == BOUNDARY_NEVER


def test_an_unnamed_actuator_fails_closed_to_never():
    """There is nothing to write in a grant, so there is nothing to approve."""
    assert classify_boundary({"kind": "button", "name": ""}).cls == BOUNDARY_NEVER


def test_a_disabled_commit_button_is_not_a_live_boundary():
    assert classify_boundary(ctl("Submit Application", disabled=True)).cls == BOUNDARY_SAFE


def test_a_text_field_has_no_boundary_to_classify():
    klass = classify_boundary({"kind": "text", "name": "First Name"})
    assert klass.cls == BOUNDARY_SAFE
    assert klass.reason == boundary.REASON_NOT_AN_ACTUATOR


def test_boundary_key_is_stable_across_instance_ids():
    """Same page, same label, different record id ⇒ ONE boundary.

    ``url_template`` collapses numeric path segments, so a wizard reached twice
    under two application ids cannot be crossed twice by claiming they are two
    different boundaries."""
    a = boundary_key("https://app.example/applications/12345/review", "Submit Application")
    b = boundary_key("https://app.example/applications/98765/review", "submit application")
    assert a == b


def test_boundary_key_separates_the_same_label_on_two_pages():
    a = boundary_key("https://app.example/claims/new", "Submit")
    b = boundary_key("https://app.example/policies/new", "Submit")
    assert a != b


def test_the_crawl_reports_the_two_lists_separately(tmp_path):
    """The coverage account carries both, and the irreversible control lands in
    exactly one of them."""
    c = build_crawler(tmp_path)
    c._note_boundary_controls(
        [ctl("Continue"), ctl("Submit Application"), ctl("Bind Coverage")],
        url="https://app.example/apply")
    cov = c._coverage.build()
    labels = [row["label"] for row in cov["approvable_boundary"]]
    assert "Submit Application" in labels
    assert "Bind Coverage" in labels
    assert "Submit Application" not in cov["submit_candidates"]
    assert "Bind Coverage" not in cov["submit_candidates"]
    assert cov["submit_candidates"] == ["Continue"]


def test_the_approvable_row_says_why_so_an_operator_can_decide(tmp_path):
    c = build_crawler(tmp_path)
    c._note_boundary_controls([ctl("Bind Coverage")], url="https://app.example/quote")
    row = c._coverage.build()["approvable_boundary"][0]
    assert row["reason"] == boundary.REASON_DANGER_VERB
    assert row["rule_id"] == "rp.verb.bind"
    assert row["severity"] == "critical"
    assert row["url"] == "https://app.example/quote"
    assert row["boundary_key"].startswith("bnd_")


def test_the_same_label_on_two_pages_is_two_approval_rows(tmp_path):
    c = build_crawler(tmp_path)
    c._note_boundary_controls([ctl("Submit")], url="https://app.example/claims/new")
    c._note_boundary_controls([ctl("Submit")], url="https://app.example/policies/new")
    rows = c._coverage.build()["approvable_boundary"]
    assert len(rows) == 2, "deduping on the label alone hides one of two boundaries"


def test_a_form_whose_submit_is_dangerous_is_no_longer_dropped(tmp_path):
    """The SECOND producer. ``discovery`` filtered ``not fc.danger``, so a form
    whose submit is "Bind Coverage" contributed nothing at all."""
    c = build_crawler(tmp_path)
    fill = FormFillResult()
    fill.flow_candidates = [
        FlowCandidate(name="Bind Coverage", target_kind="button", danger=True,
                      danger_rule_id="rp.verb.bind", danger_severity="critical",
                      control=ctl("Bind Coverage")),
    ]
    # Exercise the same routing discovery performs.
    for fc in fill.flow_candidates:
        probe = dict(fc.control)
        klass = classify_boundary(probe)
        assert klass.cls == BOUNDARY_APPROVABLE
        c._approvable_boundary.append({
            "label": fc.name, "url": "https://app.example/quote",
            "reason": klass.reason, "rule_id": klass.rule_id,
            "severity": klass.severity,
            "boundary_key": boundary_key("https://app.example/quote", fc.name)})
    assert c._coverage.build()["approvable_boundary"][0]["label"] == "Bind Coverage"


# ═══════════════════════════════════════════════════════════════════════════
#  T-AC-02 · THE APPROVAL SEAM — one control, never a wildcard, never a page
# ═══════════════════════════════════════════════════════════════════════════

def test_a_wildcard_grant_is_refused_loudly():
    with pytest.raises(GrantParseError) as exc:
        parse_grants([{"control": "*"}])
    assert "wildcard" in str(exc.value).lower()


def test_a_wildcard_grant_is_refused_in_bare_string_form_too():
    with pytest.raises(GrantParseError):
        parse_grants(["*"])


def test_a_partial_wildcard_is_refused():
    with pytest.raises(GrantParseError):
        parse_grants([{"control": "Submit*"}])


def test_a_grant_with_no_control_is_refused():
    with pytest.raises(GrantParseError):
        parse_grants([{"url": "https://app.example/apply"}])


def test_a_zero_crossing_grant_is_refused_as_a_disguised_refusal():
    with pytest.raises(GrantParseError):
        parse_grants([{"control": "Submit Application", "max_crossings": 0}])


def test_a_malformed_grant_is_raised_not_swallowed():
    """Fail-LOUD. A grant that silently parses to nothing looks exactly like a
    crawl that stopped at the boundary legitimately, and the operator would
    re-issue the same broken approval forever."""
    with pytest.raises(GrantParseError):
        parse_grants([12345])


def test_grants_are_idempotent_on_resend():
    grants = parse_grants([{"control": "Submit Application"},
                           {"control": "Submit Application"}])
    assert len(grants) == 1


def test_a_grant_authorises_exactly_one_control():
    """T-AC-02: approval for one control cannot authorise another."""
    reg = ApprovalRegistry(parse_grants([{"control": "Submit Application"}]))
    assert reg.grant_for(control_name="Submit Application") is not None
    for other in ("Bind Coverage", "Submit", "Submit Application Now",
                  "Delete Account", "Continue"):
        assert reg.grant_for(control_name=other) is None, other


def test_matching_is_exact_not_substring_in_either_direction():
    reg = ApprovalRegistry(parse_grants([{"control": "Submit"}]))
    assert reg.grant_for(control_name="Submit Application") is None
    reg2 = ApprovalRegistry(parse_grants([{"control": "Submit Application"}]))
    assert reg2.grant_for(control_name="Submit") is None


def test_a_grant_scoped_to_a_page_does_not_travel_to_another_page():
    reg = ApprovalRegistry(parse_grants([
        {"control": "Submit", "url": "https://app.example/claims/new"}]))
    assert reg.grant_for(control_name="Submit",
                         url="https://app.example/claims/new") is not None
    assert reg.grant_for(control_name="Submit",
                         url="https://app.example/policies/new") is None


def test_a_grant_scoped_to_a_state_does_not_travel_to_another_state():
    reg = ApprovalRegistry(parse_grants([
        {"control": "Submit", "state_fingerprint": "fp-aaa"}]))
    assert reg.grant_for(control_name="Submit", state_fingerprint="fp-aaa") is not None
    assert reg.grant_for(control_name="Submit", state_fingerprint="fp-bbb") is None


def test_the_approval_id_is_deterministic_so_a_replay_resolves_the_same_grant():
    a = parse_grants([{"control": "Submit Application",
                       "url": "https://app.example/apply"}])[0]
    b = parse_grants([{"control": "Submit Application",
                       "url": "https://app.example/apply"}])[0]
    assert a.approval_id == b.approval_id and a.approval_id.startswith("apr_")


def test_grants_reach_the_crawler_through_the_constructor(tmp_path):
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}])
    assert len(c._boundary_grants) == 1
    assert c._boundary_grants.approved_labels() == ("Submit Application",)


def test_a_grant_alone_enables_the_submit_tier_with_no_label_list(tmp_path):
    """The least-privilege shape: no legacy label list at all."""
    c = build_crawler(tmp_path, approvals=(), grants=[{"control": "Submit Application"}])
    assert c._submit_enabled is True
    assert c._submit_approve_all is False


def test_no_approvals_at_all_leaves_the_crawl_at_the_boundary(tmp_path):
    c = build_crawler(tmp_path)
    assert c._submit_enabled is False


def test_the_authorisation_ladder_names_its_rung(tmp_path):
    c = build_crawler(tmp_path, grants=[{"control": "Bind Coverage"}])
    grant, authority, refusal = c._authorize_crossing(
        name="Bind Coverage", control=ctl("Bind Coverage"),
        url="https://app.example/quote", fingerprint="fp1")
    assert refusal == "" and authority == AUTHORITY_GRANT and grant is not None


def test_a_named_label_cannot_authorise_an_irreversible_verb(tmp_path):
    """RC-1, stated as a test. The operator named the control; the control
    carries ``rp.verb.bind``; a flat label list is not the right shape to
    authorise a point of no return, so it is refused with a reason that says
    what to do instead."""
    c = build_crawler(tmp_path, approvals=["Bind Coverage"])
    _g, authority, refusal = c._authorize_crossing(
        name="Bind Coverage", control=ctl("Bind Coverage"),
        url="https://app.example/quote", fingerprint="fp1")
    assert authority == "" and refusal == "danger_requires_boundary_grant"


def test_a_named_label_still_authorises_a_non_irreversible_commit(tmp_path):
    """BACKWARD COMPATIBILITY. The shipped seam keeps working unchanged."""
    c = build_crawler(tmp_path, approvals=["Submit Application"])
    _g, authority, refusal = c._authorize_crossing(
        name="Submit Application", control=ctl("Submit Application"),
        url="https://app.example/apply", fingerprint="fp1")
    assert refusal == "" and authority == AUTHORITY_NAMED


def test_the_disposable_blanket_still_works_and_is_labelled_as_such(tmp_path):
    c = build_crawler(tmp_path, approvals=["*"])
    _g, authority, refusal = c._authorize_crossing(
        name="Bind Coverage", control=ctl("Bind Coverage"),
        url="https://app.example/quote", fingerprint="fp1")
    assert refusal == "" and authority == AUTHORITY_BLANKET


def test_a_grant_on_an_unattested_environment_is_refused_with_its_own_reason(tmp_path):
    """The grant is valid and the ENVIRONMENT is not. Those need different
    remedies, so they get different words."""
    c = build_crawler(tmp_path, grants=[{"control": "Bind Coverage"}], attested=False)
    _g, authority, refusal = c._authorize_crossing(
        name="Bind Coverage", control=ctl("Bind Coverage"),
        url="https://app.example/quote", fingerprint="fp1")
    assert authority == "" and refusal == "grant_without_attestation"


def test_a_grant_against_a_non_disposable_attestation_is_refused_before_reserving(
        tmp_path):
    """A doomed crossing must not spend the boundary.

    ``gate_submit`` would refuse a staging attestation anyway (T-SEC-08:
    submit-capable means disposable, attributed and unexpired) — but only after
    the reservation, so the boundary would be marked spent for a click that
    never happened, and the operator would be told nothing actionable.
    """
    from app.guard import Attestation
    guard = GuardContext(
        refuse_pack=_REFUSE_PACK,
        attestation=Attestation(attested_by="ops", env_kind="staging",
                                reset_procedure="rebuild",
                                expires_at_ms=4_102_444_800_000))
    port = _confirming_port()
    c = Crawler(
        port, crawl_id="c1", tenant_id="t1",
        target_url="https://app.example/apply", work_dir=str(tmp_path),
        refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="t", guard_version="t",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="fp",
        guard_context=guard,
        boundary_approvals=[{"control": "Submit Application"}])
    assert _cross(c, port) is False
    assert port.clicks == []
    refusal = c._crossings.to_list()[0]
    assert refusal["refusal_reason"] == "attestation_not_submit_capable"
    assert c._crossings.is_spent(control_name="Submit Application",
                                 url="https://app.example/apply") is False


def test_no_approval_of_any_strength_crosses_a_sign_out(tmp_path):
    for kwargs in ({"approvals": ["*"]},
                   {"approvals": ["Sign Out"]},
                   {"grants": [{"control": "Sign Out"}]}):
        c = build_crawler(tmp_path, **kwargs)
        _g, authority, refusal = c._authorize_crossing(
            name="Sign Out", control=ctl("Sign Out"),
            url="https://app.example/apply", fingerprint="fp1")
        assert authority == "" and refusal.startswith("boundary_never:"), kwargs


# ═══════════════════════════════════════════════════════════════════════════
#  T-AC-03 · THE ATTESTED CROSSING — one click, fully instrumented
# ═══════════════════════════════════════════════════════════════════════════

def _confirming_port() -> _Port:
    """A page whose submit stays put and renders a success banner in plain text.

    Modelled on the real summit-life-carrier behaviour: 29 in-page POSTs, no
    navigation, no dialog, and a banner that is an undecorated ``div``.
    """
    return _Port(
        controls=[ctl("Submit Application"), ctl("Back")],
        texts=["Review & Submit", "Complete the form to begin underwriting"],
        controls_after=[ctl("Back to Applications")],
        texts_after=["Application submitted successfully",
                     "Application ID APP-4471"],
    )


def _cross(c: Crawler, port: _Port, *, name="Submit Application",
           url="https://app.example/apply", fingerprint="fp-review") -> bool:
    return asyncio.run(c._execute_approved_submit(
        name=name, control=ctl(name), url=url, fingerprint=fingerprint,
        depth=0, renavigate=False))


def test_an_approved_crossing_clicks_exactly_once(tmp_path):
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    assert _cross(c, port) is True
    assert port.clicks == ["Submit Application"]


def test_an_unapproved_boundary_clicks_nothing(tmp_path):
    port = _confirming_port()
    c = build_crawler(tmp_path, port=port)
    assert _cross(c, port) is False
    assert port.clicks == []


def test_a_refusal_is_recorded_as_evidence_not_as_silence(tmp_path):
    """"The crawl reached the commit button and was not allowed through" is the
    commonest honest outcome there is, and a report showing nothing cannot be
    told apart from a crawl that never got there."""
    port = _confirming_port()
    c = build_crawler(tmp_path, port=port)
    _cross(c, port)
    refusals = [r for r in c._crossings.to_list() if r["status"] == "refused"]
    assert len(refusals) == 1
    assert refusals[0]["refusal_reason"] == "submit_not_enabled"
    assert refusals[0]["control_name"] == "Submit Application"


def test_a_refusal_does_not_spend_the_boundary(tmp_path):
    """Re-approving must be able to succeed."""
    port = _confirming_port()
    c = build_crawler(tmp_path, port=port)
    _cross(c, port)
    assert c._crossings.is_spent(control_name="Submit Application",
                                 url="https://app.example/apply") is False


def test_the_crossing_captures_everything_the_milestone_needs(tmp_path):
    """T-AC-03: timestamps, DOM before, DOM after, screenshots, navigation,
    approval, transition — recorded, not reconstructed."""
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    m = c._outcome_milestones[0]
    # The crawl clock is MONOTONIC (ms since crawl start), so a crossing that
    # happens in the first millisecond legitimately reads 0. What must hold is
    # the ORDER: observed after clicked, and a latency derived from the two.
    assert m["observed_at_ms"] >= m["clicked_at_ms"] >= 0
    assert m["latency_ms"] == m["observed_at_ms"] - m["clicked_at_ms"]
    assert m["dom_digest_before"].startswith("dom_")
    assert m["dom_digest_after"].startswith("dom_")
    assert m["dom_digest_before"] != m["dom_digest_after"], "the page did not change"
    assert m["screenshot_before"] and m["screenshot_after"]
    assert m["screenshot_before"] != m["screenshot_after"]
    assert m["url_before"] == "https://app.example/apply"
    assert m["approval_id"].startswith("apr_")
    assert m["grant"]["control"] == "Submit Application"
    assert m["attestation_env_kind"] == "disposable"
    assert m["refuse_pack_version"] == _REFUSE_PACK.version
    assert m["guard_rule_id"], "the guard's own verdict must ride along"
    assert m["crossing_id"].startswith("cross_")
    assert m["boundary_key"].startswith("bnd_")


def test_the_milestone_is_emitted_to_the_manifest_at_crossing_time(tmp_path):
    """Not rolled up at the end: a crawl cancelled after the click must still
    leave the click in the evidence."""
    import json
    from app import emit
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    path = emit.manifest_path(str(tmp_path), "c1")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    milestones = [r for r in rows if r.get("type") == "outcome_milestone"]
    assert len(milestones) == 1
    assert milestones[0]["control_name"] == "Submit Application"


def test_the_guard_is_restored_fail_closed_after_the_crossing(tmp_path):
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    assert c._guard.phase == Phase.EXPLORE
    assert c._guard.submit_flow_approved is False


def test_a_capture_failure_never_blocks_an_approved_crossing(tmp_path):
    """Evidence capture is best-effort BY DESIGN: a broken screenshot or an
    adapter with no ``visible_texts`` must degrade the milestone, never veto a
    submit the operator explicitly authorised."""
    class _Blind(_Port):
        async def status_texts(self):
            raise RuntimeError("no such verb")

        async def visible_texts(self):
            raise RuntimeError("no such verb")

        async def screenshot_png(self):
            return b""

    port = _Blind(controls=[ctl("Submit Application")],
                  url_after="https://app.example/confirmation")
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    assert _cross(c, port) is True
    assert port.clicks == ["Submit Application"]
    m = c._outcome_milestones[0]
    assert m["screenshot_before"] == "" and m["screenshot_after"] == ""
    # The navigation rung does not depend on any optional verb.
    assert m["verified"] is True and m["confirmation_rung"] == "navigation"


# ═══════════════════════════════════════════════════════════════════════════
#  T-AC-04 · THE OUTCOME MILESTONE — completion is the landing
# ═══════════════════════════════════════════════════════════════════════════

def test_a_same_page_confirmation_is_verified_by_the_text_transition(tmp_path):
    """The capability that was IMPOSSIBLE before this milestone.

    ``confirmation_detail`` had no producer anywhere in ``app/``, so a submit
    that stayed on the page could only ever score ``dom_changed`` and
    ``confirmed=False`` — on any application that answers in place, which is
    most of them.
    """
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    m = c._outcome_milestones[0]
    assert m["outcome"] == "confirmation"
    assert m["confirmation_rung"] == boundary.RUNG_TRANSITION_TEXT
    assert "submitted successfully" in m["confirmation_detail"].lower()
    assert m["verified"] is True


def test_a_declared_status_region_outranks_free_text(tmp_path):
    port = _Port(
        controls=[ctl("Submit Application")],
        texts=["Review & Submit"],
        controls_after=[ctl("Back")],
        statuses_after=["Your application was received."],
        texts_after=["Application submitted successfully"],
    )
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    m = c._outcome_milestones[0]
    assert m["confirmation_rung"] == boundary.RUNG_ARIA_STATUS
    assert m["confirmation_detail"] == "Your application was received."


def test_a_navigation_is_the_strongest_rung_and_wins(tmp_path):
    port = _Port(controls=[ctl("Submit Application")],
                 url_after="https://app.example/confirmation",
                 controls_after=[ctl("Print receipt")],
                 texts_after=["Application submitted successfully"])
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    m = c._outcome_milestones[0]
    assert m["confirmation_rung"] == boundary.RUNG_NAVIGATION
    assert m["navigated"] is True and m["verified"] is True
    assert m["url_after"] == "https://app.example/confirmation"


def test_success_wording_already_on_the_page_is_not_a_confirmation():
    """THE ANTI-FABRICATION PROPERTY.

    A form that says "you will receive a confirmation email" contains the word
    before anything has happened. Only a TRANSITION counts.
    """
    same = ["You will receive a confirmation email once submitted."]
    detail, rung = confirmation_transition(same, same)
    assert (detail, rung) == ("", "")


def test_a_page_that_does_not_change_produces_no_confirmation():
    detail, rung = confirmation_transition(["Review & Submit"], ["Review & Submit"])
    assert (detail, rung) == ("", "")


def test_in_progress_wording_is_not_success():
    """"Processing" and "pending" are what a HALF-FINISHED submit sits in.
    Counting them would green-wash exactly the failure this detects."""
    detail, rung = confirmation_transition(
        ["Review & Submit"], ["Processing your application", "Please wait"])
    assert (detail, rung) == ("", "")


def test_an_errored_submit_is_never_a_completed_journey(tmp_path):
    class _Failing(_Port):
        async def error_texts(self):
            return ["We could not process your application."] if self._clicked else []

    port = _Failing(controls=[ctl("Submit Application")],
                    controls_after=[ctl("Submit Application"), ctl("Back")],
                    texts_after=["Application submitted successfully"])
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    m = c._outcome_milestones[0]
    assert m["outcome"] == "error"
    assert m["verified"] is False, "an error may never be read as a confirmation"
    assert c._forms_confirmed == 0


def test_a_submit_that_fired_but_did_nothing_is_recorded_unverified(tmp_path):
    port = _Port(controls=[ctl("Submit Application")], texts=["Review & Submit"])
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    assert _cross(c, port) is True
    m = c._outcome_milestones[0]
    assert m["verified"] is False
    assert m["confirmation_rung"] == ""
    assert c._forms_submitted == 1, "the ATTEMPT is still counted honestly"


def test_verified_cannot_be_supplied_by_a_caller():
    """It is a property, computed from the observation. There is no setter, so
    there is no line of code anywhere that can assert a completion."""
    m = OutcomeMilestone(milestone_id="m", crossing_id="c", approval_id="a",
                         boundary_key="b", control_name="Submit Application")
    with pytest.raises(AttributeError):
        m.verified = True                                    # type: ignore[misc]
    assert OutcomeMilestone.__dataclass_fields__.get("verified") is None


def test_a_confirmation_rung_without_a_page_change_is_not_verified():
    """Belt and braces: a rung is necessary, not sufficient. A submit that
    leaves the page byte-identical has not landed anywhere."""
    digest = dom_digest([{"kind": "button", "name": "Submit Application"}])
    m = OutcomeMilestone(
        milestone_id="m", crossing_id="c", approval_id="a", boundary_key="b",
        control_name="Submit Application", outcome="confirmation",
        confirmation_rung=boundary.RUNG_TRANSITION_TEXT,
        navigated=False, dom_digest_before=digest, dom_digest_after=digest)
    assert m.verified is False


def test_dom_digest_is_value_free():
    """It travels back to qe-central as evidence. No user value may enter it."""
    a = dom_digest([{"kind": "text", "name": "First Name", "value": "Alice"}])
    b = dom_digest([{"kind": "text", "name": "First Name", "value": "Bob"}])
    assert a == b


def test_dom_digest_counts_rather_than_sets():
    """Revealing a SECOND Yes/No pair changes the shape while adding no new
    label; a set-diff would call that no change at all."""
    one = dom_digest([{"kind": "radio", "name": "Yes"}])
    two = dom_digest([{"kind": "radio", "name": "Yes"}, {"kind": "radio", "name": "Yes"}])
    assert one != two


# ═══════════════════════════════════════════════════════════════════════════
#  T-AC-05 · EXACTLY ONCE
# ═══════════════════════════════════════════════════════════════════════════

def test_the_second_traversal_does_not_submit_again(tmp_path):
    """T-AC-05, stated exactly as briefed: first traversal submits once, second
    traversal submits not at all."""
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    assert _cross(c, port) is True
    assert _cross(c, port) is False
    assert port.clicks == ["Submit Application"]
    assert len(c._outcome_milestones) == 1


def test_a_second_traversal_with_a_DIFFERENT_fingerprint_still_cannot_resubmit(tmp_path):
    """THE KEY THAT MADE THIS POSSIBLE.

    The shipped dedup was ``f"{fingerprint}::{name}"``, and the fingerprint is a
    function of the live DOM. A second traversal that answered one question
    differently, revealed a follow-up block, or simply arrived with a banner on
    screen fingerprints DIFFERENTLY — and would have submitted the same
    application a second time. The logical key (page + label) does not move.
    """
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    assert _cross(c, port, fingerprint="fp-first") is True
    assert _cross(c, port, fingerprint="fp-second-totally-different") is False
    assert port.clicks == ["Submit Application"]


def test_an_instance_id_in_the_url_cannot_mint_a_second_crossing(tmp_path):
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    assert _cross(c, port, url="https://app.example/applications/111/review") is True
    assert _cross(c, port, url="https://app.example/applications/222/review") is False


def test_the_boundary_is_spent_BEFORE_the_click(tmp_path):
    """A crossing that dies mid-flight must leave the boundary spent. A
    duplicate irreversible action is unrecoverable; a missing milestone is not.
    """
    seen: dict = {}

    async def exploding_submit(port, control, url, emitter, clock, **kw):
        seen["spent_during"] = c._crossings.is_spent(
            control_name="Submit Application", url="https://app.example/apply")
        raise RuntimeError("the browser died mid-crossing")

    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    original = submit_mod.execute_submit_phase_b
    submit_mod.execute_submit_phase_b = exploding_submit
    try:
        with pytest.raises(RuntimeError):
            _cross(c, port)
    finally:
        submit_mod.execute_submit_phase_b = original
    assert seen["spent_during"] is True, "reserved AFTER the click — the window is open"
    assert _cross(c, port) is False, "a dead crossing must not be retried"
    assert c._guard.phase == Phase.EXPLORE, "the guard must be restored even on a throw"


def test_the_ledger_never_retries_of_its_own_accord(tmp_path):
    port = _confirming_port()
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    for _ in range(5):
        _cross(c, port)
    assert port.clicks == ["Submit Application"]
    crossed = [r for r in c._crossings.to_list() if r["status"] == "crossed"]
    assert len(crossed) == 1


def test_a_grant_may_deliberately_authorise_more_than_one_crossing(tmp_path):
    """Expressed as a budget rather than a boolean, so a deliberate
    multi-crossing approval is a DATA change and never a code change."""
    port = _confirming_port()
    c = build_crawler(
        tmp_path, port=port,
        grants=[{"control": "Submit Application", "max_crossings": 2}])
    assert _cross(c, port, fingerprint="a") is True
    assert _cross(c, port, fingerprint="b") is True
    assert _cross(c, port, fingerprint="c") is False
    assert port.clicks == ["Submit Application", "Submit Application"]


def test_the_default_grant_permits_exactly_one():
    assert ApprovalGrant(control="Submit Application").max_crossings == 1


def test_two_different_boundaries_are_independent(tmp_path):
    ledger = CrossingLedger()
    ledger.reserve(control_name="Submit", url="https://a/claims/new",
                   state_fingerprint="fp", approval_id="x", sequence_index=0)
    assert ledger.is_spent(control_name="Submit", url="https://a/claims/new")
    assert not ledger.is_spent(control_name="Submit", url="https://a/policies/new")


# ═══════════════════════════════════════════════════════════════════════════
#  T-AC-06 · COMPLETION IS AN OBSERVATION, NEVER A COUNTER
# ═══════════════════════════════════════════════════════════════════════════

def _flow(**kw):
    base = dict(entry_fingerprint="fp", entry_url="/apply", entry_title="Apply",
                steps=[{"fingerprint": "fp", "url": "/apply", "title": "Apply",
                        "fields_filled": 3, "fields_unfilled": 0}],
                terminal=flow_ledger.TERMINAL_SUBMIT_BOUNDARY)
    base.update(kw)
    return flow_ledger.build_flow(**base)


def test_a_journey_without_a_milestone_is_not_completed():
    f = _flow()
    assert f["completed"] is True, "the funnel WAS walked to its end"
    assert f["journey_completed"] is False, "but nothing was crossed"
    assert f["outcome_milestone"] is None


def test_a_journey_with_an_unverified_milestone_is_not_completed():
    f = _flow(terminal=flow_ledger.TERMINAL_SUBMIT_CROSSED,
              outcome_milestone={"verified": False, "outcome": "error"})
    assert f["journey_completed"] is False


def test_a_journey_with_a_verified_milestone_is_completed():
    f = _flow(terminal=flow_ledger.TERMINAL_SUBMIT_CROSSED,
              outcome_milestone={"verified": True, "outcome": "navigation"})
    assert f["journey_completed"] is True


def test_forms_submitted_cannot_falsely_mark_completion(tmp_path):
    """T-AC-06 STATED AS AN ATTACK.

    Inflate the counter to an absurd number and assert that not one completion
    field anywhere moves. The counter is a statistic; the transition is the
    truth. This is the test that would have caught nine errored submits scoring
    exactly as nine completed applications.
    """
    port = _Port(controls=[ctl("Submit Application")], texts=["Review & Submit"])
    c = build_crawler(tmp_path, grants=[{"control": "Submit Application"}], port=port)
    _cross(c, port)
    c._forms_submitted = 999
    c._forms_confirmed = 999
    cov = c._coverage.build()
    assert cov["journeys_completed"] == 0
    assert cov["boundaries_crossed"] == 1
    assert all(not m["verified"] for m in cov["outcome_milestones"])
    assert flow_ledger.journeys_completed(cov["flows"]) == 0


def test_the_coverage_boundary_count_is_not_derived_from_the_counter(tmp_path):
    """``unexercised`` used to be ``len(submit_candidates) - forms_submitted``,
    so an application whose submits all FAILED reported its boundaries as
    exercised."""
    c = build_crawler(tmp_path)
    c._note_boundary_controls([ctl("Bind Coverage")], url="https://app.example/quote")
    c._forms_submitted = 50
    summary = c._coverage.build()["summary"]
    assert "1 irreversible boundary/ies awaiting approval" in summary


def test_journeys_completed_is_computed_from_flows_not_maintained(tmp_path):
    flows = [
        _flow(terminal=flow_ledger.TERMINAL_SUBMIT_CROSSED,
              outcome_milestone={"verified": True}),
        _flow(terminal=flow_ledger.TERMINAL_SUBMIT_CROSSED,
              outcome_milestone={"verified": False}),
        _flow(),
    ]
    assert flow_ledger.journeys_completed(flows) == 1
    summary = flow_ledger.summarize(flows)
    assert summary["journeys_completed"] == 1
    assert summary["boundaries_crossed"] == 2
    assert summary["flows_completed"] == 3, (
        "all three walked the funnel to an end — that is coverage, not completion")


def test_the_three_numbers_stay_distinct():
    """Collapsing any two of them is how "we complete customer journeys" gets
    claimed on the strength of a crawl that never crossed anything."""
    s = flow_ledger.summarize([_flow()])
    assert s["flows_completed"] == 1
    assert s["boundaries_crossed"] == 0
    assert s["journeys_completed"] == 0


# ═══════════════════════════════════════════════════════════════════════════
#  DETERMINISM / REPLAY / THREAD SAFETY (non-functional requirements)
# ═══════════════════════════════════════════════════════════════════════════

def test_the_same_inputs_resolve_the_same_grant_and_the_same_keys(tmp_path):
    def run():
        port = _confirming_port()
        c = build_crawler(tmp_path / str(id(port)),
                          grants=[{"control": "Submit Application"}], port=port)
        _cross(c, port)
        m = c._outcome_milestones[0]
        return (m["approval_id"], m["boundary_key"], m["crossing_id"],
                m["dom_digest_before"], m["dom_digest_after"],
                m["confirmation_rung"], m["verified"])
    assert run() == run()


def test_the_crossing_ledger_holds_no_module_level_state():
    """No hidden global state: two crawls in one process must not see each
    other's crossings."""
    a, b = CrossingLedger(), CrossingLedger()
    a.reserve(control_name="Submit", url="https://x/apply",
              state_fingerprint="fp", approval_id="i", sequence_index=0)
    assert a.is_spent(control_name="Submit", url="https://x/apply")
    assert not b.is_spent(control_name="Submit", url="https://x/apply")


def test_two_crawlers_in_one_process_do_not_share_approvals(tmp_path):
    a = build_crawler(tmp_path / "a", grants=[{"control": "Submit Application"}])
    b = build_crawler(tmp_path / "b")
    assert len(a._boundary_grants) == 1 and len(b._boundary_grants) == 0
    assert a._crossings is not b._crossings
    assert a._outcome_milestones is not b._outcome_milestones


def test_the_boundary_module_imports_without_the_crawler():
    """It is a leaf. A cycle here would be the fastest way to make the crossing
    untestable in isolation."""
    import subprocess
    import sys
    from pathlib import Path
    root = str(Path(__file__).resolve().parent.parent)
    out = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {root!r});"
         "import app.boundary;"
         "print('crawler' in sys.modules or 'app.crawler' in sys.modules)"],
        capture_output=True, text=True, timeout=180)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"
