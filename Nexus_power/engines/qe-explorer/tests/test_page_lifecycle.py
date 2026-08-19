"""M1.5 — the page-lifecycle POLICY, proven without a browser (T-ND-01…T-ND-04).

Everything here runs in milliseconds and needs no Chromium, because the
decisions themselves are pure: :mod:`app.page_lifecycle` takes what was observed
about a dialog / a popup / a download and returns a decision plus a reason.  The
Playwright half — that the decisions are actually reached, executed and
recorded against a real browser — is proven by
``tests/browser/test_page_lifecycle_execution.py``.

The two halves are deliberately separate.  A policy that is only ever exercised
through a browser is a policy nobody can enumerate the branches of, and every
one of these branches is a decision about whether a real application's data gets
mutated.
"""
from __future__ import annotations

import pytest

from app import page_lifecycle as pl
from app.fingerprint import state_fingerprint
from app.state_identity import StateFingerprinter

# ─── T-ND-02 · dialog intent ─────────────────────────────────────────────────


class TestFunnelConfirmations:
    """The bug M1.5 exists to close: an accepted confirm must let the funnel move."""

    @pytest.mark.parametrize("message", [
        "Are you sure you want to continue?",
        "Submit this application for review?",
        "Proceed with the quote?",
        "Please confirm to continue.",
        "",                                   # a confirm with no message at all
    ])
    def test_a_funnel_confirm_is_accepted(self, message: str) -> None:
        decision = pl.resolve_dialog(dialog_type="confirm", message=message,
                                     control_label="Continue", action_verb="click")
        assert decision.action == pl.ACTION_ACCEPT
        assert decision.intent == pl.INTENT_FUNNEL_CONFIRMATION
        assert decision.reason, "a decision with no reason is not auditable"

    def test_the_control_label_is_an_input_not_just_the_message(self) -> None:
        """Intent resolution is not string-matching a message.

        The SAME bland message behind two different controls is two different
        questions, and only the adapter — which performed the click — knows
        which one asked it.
        """
        forward = pl.resolve_dialog(dialog_type="confirm", message="Are you sure?",
                                    control_label="Continue")
        destructive = pl.resolve_dialog(dialog_type="confirm", message="Are you sure?",
                                        control_label="Delete Policy")
        assert forward.action == pl.ACTION_ACCEPT
        assert destructive.action == pl.ACTION_DISMISS
        assert destructive.intent == pl.INTENT_DESTRUCTIVE_CONFIRMATION


class TestLeaveWarnings:
    """Accepting one abandons the journey — the opposite failure to auto-dismiss."""

    @pytest.mark.parametrize("message", [
        "Are you sure you want to leave this page?",
        "Leave site? Changes you made may not be saved.",
        "You have unsaved changes.",
        "Your changes will be lost.",
        "Navigate away from this application?",
    ])
    def test_a_leave_warning_is_dismissed(self, message: str) -> None:
        decision = pl.resolve_dialog(dialog_type="confirm", message=message,
                                     control_label="Return to dashboard")
        assert decision.action == pl.ACTION_DISMISS
        assert decision.intent == pl.INTENT_LEAVE_WARNING

    def test_beforeunload_is_dismissed_whatever_it_says(self) -> None:
        """Playwright's ``accept()`` on a beforeunload means LEAVE THE PAGE."""
        decision = pl.resolve_dialog(dialog_type="beforeunload", message="",
                                     control_label="Continue")
        assert decision.action == pl.ACTION_DISMISS
        assert decision.intent == pl.INTENT_LEAVE_WARNING

    def test_a_leave_warning_wins_over_a_destructive_word(self) -> None:
        """"Discard your changes and leave?" is a leave warning first.

        Both lexicons match; the ORDER of the ladder decides, and it must decide
        the same way every time — so it is pinned rather than left to whichever
        check happens to run first after an edit.
        """
        decision = pl.resolve_dialog(
            dialog_type="confirm",
            message="Discard your changes and leave this page?",
            control_label="Back")
        assert decision.intent == pl.INTENT_LEAVE_WARNING

    def test_a_forward_label_does_not_override_a_leave_message(self) -> None:
        decision = pl.resolve_dialog(
            dialog_type="confirm",
            message="Are you sure you want to leave this page?",
            control_label="Continue")
        assert decision.action == pl.ACTION_DISMISS


class TestDestructiveConfirmations:
    """A native confirm() is not an approved crossing; an A4.3 grant is."""

    @pytest.mark.parametrize("message", [
        "Delete this application permanently?",
        "This cannot be undone. Continue?",
        "Withdraw your application?",
        "Close your account?",
    ])
    def test_refused_without_an_operator_approval(self, message: str) -> None:
        decision = pl.resolve_dialog(dialog_type="confirm", message=message,
                                     control_label="Delete Application")
        assert decision.action == pl.ACTION_DISMISS
        assert decision.intent == pl.INTENT_DESTRUCTIVE_CONFIRMATION
        assert "no operator approval" in decision.reason

    def test_accepted_under_a_named_operator_approval(self) -> None:
        decision = pl.resolve_dialog(
            dialog_type="confirm", message="Delete this application permanently?",
            control_label="Delete Application", journey_phase="submit",
            approved_labels=("Delete Application",))
        assert decision.action == pl.ACTION_ACCEPT
        assert "operator approval" in decision.reason
        assert "submit" in decision.reason, "the phase the grant fired in is evidence"

    def test_a_blanket_approval_covers_it(self) -> None:
        decision = pl.resolve_dialog(
            dialog_type="confirm", message="Delete this application permanently?",
            control_label="Delete Application", approved_labels=("*",))
        assert decision.action == pl.ACTION_ACCEPT

    def test_an_approval_for_a_different_control_does_not_transfer(self) -> None:
        decision = pl.resolve_dialog(
            dialog_type="confirm", message="Delete this application permanently?",
            control_label="Delete Application",
            approved_labels=("Submit Application",))
        assert decision.action == pl.ACTION_DISMISS

    def test_observe_only_overrules_even_an_approval(self) -> None:
        """Posture RAISES the floor and is never lowered by a grant."""
        decision = pl.resolve_dialog(
            dialog_type="confirm", message="Delete this application permanently?",
            control_label="Delete Application", approved_labels=("*",),
            observe_only=True)
        assert decision.action == pl.ACTION_DISMISS


class TestOtherDialogTypes:

    def test_an_alert_is_acknowledged(self) -> None:
        """One button — but the page stays BLOCKED until it is answered."""
        decision = pl.resolve_dialog(dialog_type="alert",
                                     message="Your session expires in five minutes.")
        assert decision.action == pl.ACTION_ACCEPT
        assert decision.intent == pl.INTENT_NOTICE

    def test_a_prompt_is_declined_rather_than_answered_with_invented_input(self) -> None:
        decision = pl.resolve_dialog(dialog_type="prompt",
                                     message="Enter your agent reference code")
        assert decision.action == pl.ACTION_DISMISS
        assert decision.intent == pl.INTENT_PROMPT_UNANSWERABLE

    def test_an_unknown_dialog_type_is_treated_as_a_confirm(self) -> None:
        decision = pl.resolve_dialog(dialog_type="something-new",
                                     message="Continue?", control_label="Continue")
        assert decision.action == pl.ACTION_ACCEPT

    def test_observe_only_dismisses_an_ordinary_funnel_confirm(self) -> None:
        decision = pl.resolve_dialog(dialog_type="confirm",
                                     message="Are you sure you want to continue?",
                                     control_label="Continue", observe_only=True)
        assert decision.action == pl.ACTION_DISMISS
        assert "observe-only" in decision.reason

    def test_an_alert_is_still_acknowledged_under_observe_only(self) -> None:
        """It commits nothing and the page cannot proceed until it is answered."""
        decision = pl.resolve_dialog(dialog_type="alert", message="Heads up.",
                                     observe_only=True)
        assert decision.action == pl.ACTION_ACCEPT


def test_every_dialog_decision_is_one_of_two_actions() -> None:
    """No input may produce a third answer, because there isn't one: a dialog
    left unanswered blocks the page forever."""
    messages = ["", "Continue?", "Leave this page?", "Delete permanently?",
                "\u00a1Hola!", "x" * 4000]
    for dtype in (pl.DIALOG_ALERT, pl.DIALOG_CONFIRM, pl.DIALOG_PROMPT,
                  pl.DIALOG_BEFOREUNLOAD, "", "weird"):
        for message in messages:
            for observe_only in (True, False):
                d = pl.resolve_dialog(dialog_type=dtype, message=message,
                                      observe_only=observe_only)
                assert d.action in (pl.ACTION_ACCEPT, pl.ACTION_DISMISS)
                assert d.intent and d.reason


# ─── T-ND-01 · popup adoption ────────────────────────────────────────────────


class TestPopupAdoption:

    def test_an_in_scope_navigated_popup_is_adopted(self) -> None:
        decision = pl.resolve_popup(popup_url="https://app.example.com/details",
                                    opener_url="https://app.example.com/quote")
        assert decision.adopt
        assert "details" in decision.reason

    @pytest.mark.parametrize("url", ["about:blank", "", "  ", "ABOUT:BLANK"])
    def test_a_popup_that_never_navigated_is_retained(self, url: str) -> None:
        decision = pl.resolve_popup(popup_url=url, opener_url="https://app.example.com/")
        assert not decision.adopt
        assert pl.POPUP_BLANK in decision.reason

    def test_a_foreign_origin_popup_is_recorded_but_never_adopted(self) -> None:
        """The same gate ``_expand`` applies to an off-domain redirect.

        Inventorying a partner site, an IdP or a payment processor as the
        application's own substrate attributes someone else's UI to the app.
        """
        decision = pl.resolve_popup(popup_url="https://partner.example.net/x",
                                    opener_url="https://app.example.com/",
                                    in_scope=False)
        assert not decision.adopt
        assert pl.POPUP_OUT_OF_SCOPE in decision.reason

    def test_a_popup_closed_before_observation_is_retained(self) -> None:
        decision = pl.resolve_popup(popup_url="https://app.example.com/x", closed=True)
        assert not decision.adopt
        assert pl.POPUP_CLOSED in decision.reason

    def test_the_first_usable_popup_in_a_batch_wins(self) -> None:
        """Deterministic when several open at once — and the losers are still
        recorded, so "which one did it take and why" is answerable."""
        first = pl.resolve_popup(popup_url="https://app.example.com/a")
        second = pl.resolve_popup(popup_url="https://app.example.com/b",
                                  already_adopted_this_batch=True)
        assert first.adopt and not second.adopt
        assert pl.POPUP_SUPERSEDED in second.reason

    def test_a_closed_popup_is_judged_closed_even_if_it_has_a_url(self) -> None:
        decision = pl.resolve_popup(popup_url="https://app.example.com/x",
                                    closed=True, in_scope=True)
        assert pl.POPUP_CLOSED in decision.reason


# ─── The registry: exactly one ACTIVE page, and how each got there ───────────


class _FakePage:
    """A stand-in handle.  The registry never calls a method on a page — which
    is what lets this test hold the lifecycle with no browser at all."""

    def __init__(self, name: str) -> None:
        self.name = name


class TestPageRegistry:

    def test_the_primary_page_carries_the_empty_token(self) -> None:
        """LOAD-BEARING.  The empty primary token is what keeps every
        fingerprint recorded before M1.5 reproducing byte for byte."""
        reg = pl.PageRegistry()
        primary = _FakePage("primary")
        entry = reg.register_primary(primary, url="https://app.example.com/")
        assert entry.token == ""
        assert reg.active_token() == ""
        assert entry.lifecycle == pl.LIFECYCLE_ACTIVE

    def test_adopted_pages_get_distinct_tokens_in_creation_order(self) -> None:
        reg = pl.PageRegistry()
        reg.register_primary(_FakePage("primary"))
        a, b = _FakePage("a"), _FakePage("b")
        assert reg.register(a).token == "p1"
        assert reg.register(b).token == "p2"
        assert reg.register(a).token == "p1", "registration must be idempotent"

    def test_adoption_swaps_exactly_one_active_page(self) -> None:
        reg = pl.PageRegistry()
        primary = _FakePage("primary")
        popup = _FakePage("popup")
        reg.register_primary(primary)
        reg.register(popup)
        reg.adopt(popup, reason="target=_blank")

        assert reg.active_token() == "p1"
        assert reg.get(primary).lifecycle == pl.LIFECYCLE_SWAPPED
        assert reg.get(popup).lifecycle == pl.LIFECYCLE_ACTIVE
        actives = [e for e in reg.entries() if e.lifecycle == pl.LIFECYCLE_ACTIVE]
        assert len(actives) == 1, "two authoritative pages is the ambiguous global"

    def test_a_retained_page_never_becomes_authoritative(self) -> None:
        reg = pl.PageRegistry()
        reg.register_primary(_FakePage("primary"))
        foreign = _FakePage("foreign")
        reg.register(foreign)
        reg.retain(foreign, reason="out_of_scope")
        assert reg.get(foreign).lifecycle == pl.LIFECYCLE_RETAINED
        assert reg.active_token() == ""

    def test_retain_cannot_demote_the_active_page(self) -> None:
        reg = pl.PageRegistry()
        popup = _FakePage("popup")
        reg.register_primary(_FakePage("primary"))
        reg.register(popup)
        reg.adopt(popup, reason="adopted")
        reg.retain(popup, reason="mistake")
        assert reg.get(popup).lifecycle == pl.LIFECYCLE_ACTIVE

    def test_closing_the_active_page_leaves_nothing_active_until_promotion(self) -> None:
        reg = pl.PageRegistry()
        primary = _FakePage("primary")
        popup = _FakePage("popup")
        reg.register_primary(primary)
        reg.register(popup)
        reg.adopt(popup, reason="adopted")
        reg.close(popup)
        assert not reg.has_active()
        candidates = reg.candidates_for_promotion()
        assert candidates and candidates[-1].handle is primary

    def test_promotion_candidates_are_newest_first_and_exclude_the_active_page(self) -> None:
        """Newest first: the most recently opened page is the one the
        application most recently intended the user to be looking at.  The
        ACTIVE page is never its own replacement."""
        reg = pl.PageRegistry()
        reg.register_primary(_FakePage("primary"))
        older, newer = _FakePage("older"), _FakePage("newer")
        reg.register(older)
        reg.register(newer)
        # primary is active, so it is not a candidate to replace itself.
        assert [e.token for e in reg.candidates_for_promotion()] == ["p2", "p1"]
        # Once a popup takes over, the primary becomes the fallback again.
        reg.adopt(newer, reason="adopted")
        assert [e.token for e in reg.candidates_for_promotion()] == ["p1", ""]

    def test_a_closed_page_is_never_a_promotion_candidate(self) -> None:
        reg = pl.PageRegistry()
        reg.register_primary(_FakePage("primary"))
        dead = _FakePage("dead")
        reg.register(dead)
        reg.close(dead)
        assert dead not in [e.handle for e in reg.candidates_for_promotion()]

    def test_adopting_a_closed_page_is_refused(self) -> None:
        reg = pl.PageRegistry()
        primary = _FakePage("primary")
        dead = _FakePage("dead")
        reg.register_primary(primary)
        reg.register(dead)
        reg.close(dead)
        assert reg.adopt(dead, reason="should not happen") is None
        assert reg.active_token() == ""

    def test_the_snapshot_is_all_strings_and_bounded(self) -> None:
        reg = pl.PageRegistry()
        reg.register_primary(_FakePage("primary"), url="https://app.example.com/" + "x" * 900)
        rows = reg.snapshot()
        assert rows and all(isinstance(v, str) for row in rows for v in row.values())
        assert all(len(row["url"]) <= 500 for row in rows)


# ─── T-ND-03 · artifact naming ───────────────────────────────────────────────


class TestArtifactNaming:

    @pytest.mark.parametrize("hostile", [
        "../../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "/absolute/path/report.pdf",
        "C:\\Users\\victim\\report.pdf",
        "....//....//escape.txt",
    ])
    def test_a_hostile_suggested_name_cannot_escape_the_directory(self, hostile: str) -> None:
        """A download's ``suggested_filename`` is APPLICATION-controlled text."""
        name = pl.safe_artifact_name(hostile, index=1)
        assert "/" not in name and "\\" not in name
        assert not name.startswith(".")
        assert ".." not in name.replace("...", "")

    def test_two_downloads_with_the_same_name_cannot_overwrite_each_other(self) -> None:
        """Silently destroying the first artifact while the manifest claims both
        were captured is a worse failure than not capturing at all."""
        first = pl.safe_artifact_name("report.pdf", index=1)
        second = pl.safe_artifact_name("report.pdf", index=2)
        assert first != second
        assert first.endswith("report.pdf") and second.endswith("report.pdf")

    def test_an_empty_or_unusable_name_still_produces_a_file_name(self) -> None:
        for raw in ("", "   ", "///", "...", None):
            name = pl.safe_artifact_name(raw, index=7)
            assert name.startswith("007_") and len(name) > 4

    def test_the_name_is_bounded(self) -> None:
        assert len(pl.safe_artifact_name("a" * 5000, index=1)) <= 104

    @pytest.mark.parametrize("filename,expected", [
        ("policy.pdf", "application/pdf"),
        ("schedule.CSV", "text/csv"),
        ("export.xlsx",
         "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("mystery.bin", ""),
    ])
    def test_content_type_falls_back_to_the_suffix(self, filename: str,
                                                   expected: str) -> None:
        assert pl.content_type_for(filename) == expected

    def test_a_declared_content_type_wins_over_the_suffix(self) -> None:
        assert pl.content_type_for("report.pdf", "text/csv; charset=utf-8") == "text/csv"


# ─── Evidence records ────────────────────────────────────────────────────────


class TestEvidenceRecords:

    def test_a_popup_record_carries_everything_the_milestone_asks_for(self) -> None:
        decision = pl.resolve_popup(popup_url="https://app.example.com/details",
                                    opener_url="https://app.example.com/quote")
        record = pl.popup_record(
            opener_url="https://app.example.com/quote",
            popup_url="https://app.example.com/details", token="p1",
            decision=decision, timestamp_ms=1234, trigger_label="Open Details")
        for key in ("event", "opener_url", "popup_url", "page_token",
                    "disposition", "adopted", "reason", "timestamp_ms"):
            assert key in record, f"popup evidence is missing {key!r}"
        assert record["event"] == pl.EVENT_POPUP and record["adopted"] is True

    def test_a_dialog_record_explains_the_decision(self) -> None:
        decision = pl.resolve_dialog(dialog_type="confirm", message="Continue?",
                                     control_label="Continue")
        record = pl.dialog_record(dialog_type="confirm", message="Continue?",
                                  decision=decision, timestamp_ms=1,
                                  page_url="https://app.example.com/",
                                  control_label="Continue", action_verb="click",
                                  journey_phase="walk")
        for key in ("dialog_type", "message", "action", "intent", "reason",
                    "page_url", "page_token", "control_label", "journey_phase"):
            assert key in record, f"dialog evidence is missing {key!r}"
        assert record["action"] == pl.ACTION_ACCEPT

    def test_a_download_record_distinguishes_a_claim_from_an_artifact(self) -> None:
        """"A download happened" and "a file exists" are different claims."""
        empty = pl.download_record(
            suggested_filename="report.pdf", source_url="https://app.example.com/r",
            page_url="https://app.example.com/", artifact_path="", bytes_written=0,
            timestamp_ms=1, error="save_as failed")
        real = pl.download_record(
            suggested_filename="report.pdf", source_url="https://app.example.com/r",
            page_url="https://app.example.com/",
            artifact_path="artifacts/001_report.pdf", bytes_written=685,
            timestamp_ms=1)
        assert empty["captured"] is False and empty["bytes"] == 0
        assert real["captured"] is True and real["bytes"] == 685
        assert real["content_type"] == "" or real["content_type"] == "application/pdf"

    def test_records_are_bounded_so_an_application_cannot_flood_the_manifest(self) -> None:
        decision = pl.resolve_dialog(dialog_type="alert", message="x" * 10_000)
        record = pl.dialog_record(dialog_type="alert", message="x" * 10_000,
                                  decision=decision, timestamp_ms=1,
                                  page_url="u" * 10_000)
        assert len(record["message"]) <= 500
        assert len(record["page_url"]) <= 2000
        assert len(record["reason"]) <= 500


# ─── T-ND-04 · state identity after a page swap ──────────────────────────────


def _controls(*names: str) -> list[dict[str, str]]:
    return [{"role": "button", "name": n, "kind": "button"} for n in names]


class TestPageTokenIdentity:
    """The two failures the milestone names pull in OPPOSITE directions:
    identity must FOLLOW the adopted page, and must not FRACTURE because a
    Playwright object changed.  Both are asserted here."""

    def test_a_single_page_crawl_is_byte_identical_to_before_m15(self) -> None:
        """The compatibility guarantee the whole design rests on.

        Every fingerprint ever persisted was computed with no page token.  If
        the primary page contributed one, the entire recorded corpus would
        re-key on the next crawl.
        """
        fp = StateFingerprinter()
        url, controls = "https://app.example.com/quote", _controls("Continue", "Back")
        assert fp.fingerprint(url=url, controls=controls) == \
            state_fingerprint(url, controls, ())
        assert fp.fingerprint(url=url, controls=controls, page_token="") == \
            state_fingerprint(url, controls, ())

    def test_a_popup_identical_to_its_opener_gets_a_DISTINCT_identity(self) -> None:
        """THE T-ND-04 CASE.  ``window.open(location.href)``: same URL template,
        same interactive controls, same dialogs.  Every DOM signal the base
        fingerprint reads collapses, so without the page identity the popup
        silently inherits the opener's fingerprint — which is the crawler
        fingerprinting the page it has already left."""
        fp = StateFingerprinter()
        url, controls = "https://app.example.com/quote", _controls("Continue", "Back")
        opener = fp.fingerprint(url=url, controls=controls)
        popup = fp.fingerprint(url=url, controls=controls, page_token="p1")
        assert popup != opener, "the popup inherited the stale page's identity"

    def test_the_adopted_page_keeps_ONE_identity_for_its_own_state(self) -> None:
        """Re-observing the same state on the same adopted page must not mint a
        new identity — that is the fracture failure, and it is what a bare
        counter would produce."""
        fp = StateFingerprinter()
        url, controls = "https://app.example.com/quote", _controls("Continue")
        fp.fingerprint(url=url, controls=controls)                       # opener
        first = fp.fingerprint(url=url, controls=controls, page_token="p1")
        second = fp.fingerprint(url=url, controls=controls, page_token="p1")
        assert first == second

    def test_the_opener_keeps_its_identity_after_a_popup_was_adopted(self) -> None:
        fp = StateFingerprinter()
        url, controls = "https://app.example.com/quote", _controls("Continue")
        before = fp.fingerprint(url=url, controls=controls)
        fp.fingerprint(url=url, controls=controls, page_token="p1")
        after = fp.fingerprint(url=url, controls=controls)
        assert before == after

    def test_a_popup_with_a_DIFFERENT_page_is_already_distinct_and_takes_no_token(self) -> None:
        """No token is folded in when the DOM already separates the two states —
        so an adopted page's ordinary states hash exactly as they always did,
        and a state reached first by the popup and later by the opener is ONE
        state, not two."""
        fp = StateFingerprinter()
        details = "https://app.example.com/details"
        controls = _controls("Accept Quote", "Decline Quote")
        via_popup = fp.fingerprint(url=details, controls=controls, page_token="p1")
        assert via_popup == state_fingerprint(details, controls, ())
        via_opener = fp.fingerprint(url=details, controls=controls, page_token="")
        assert via_opener == via_popup, "the same state reached twice is one state"

    def test_query_parameters_that_matter_still_separate_two_pages(self) -> None:
        """A popup carrying a pagination parameter the opener does not is
        distinct on the URL alone — the token is never needed and never used."""
        fp = StateFingerprinter()
        controls = _controls("Next")
        a = fp.fingerprint(url="https://app.example.com/list?page=1", controls=controls)
        b = fp.fingerprint(url="https://app.example.com/list?page=2",
                           controls=controls, page_token="p1")
        assert a != b
        assert b == state_fingerprint("https://app.example.com/list?page=2", controls, ())

    def test_two_different_adopted_pages_showing_one_shape_stay_two_states(self) -> None:
        fp = StateFingerprinter()
        url, controls = "https://app.example.com/quote", _controls("Continue")
        digests = {
            fp.fingerprint(url=url, controls=controls, page_token=t)
            for t in ("", "p1", "p2")
        }
        assert len(digests) == 3

    def test_the_claim_map_is_bounded(self) -> None:
        """A long crawl degrades to pre-M1.5 behaviour, never to a memory leak."""
        fp = StateFingerprinter()
        limit = StateFingerprinter._MAX_PAGE_CLAIMS
        for i in range(limit + 50):
            fp.fingerprint(url=f"https://app.example.com/s{i}", controls=_controls("Go"))
        assert len(fp._page_claims) <= limit

    def test_the_hasher_itself_ignores_an_empty_token(self) -> None:
        controls = _controls("Continue")
        assert state_fingerprint("https://a/b", controls, (), page_token="") == \
            state_fingerprint("https://a/b", controls, ())
        assert state_fingerprint("https://a/b", controls, (), page_token="   ") == \
            state_fingerprint("https://a/b", controls, ())


def test_lifecycle_states_are_the_documented_five_plus_created() -> None:
    """The lifecycle is a named contract, not an ad-hoc set of strings."""
    assert set(pl.LIFECYCLE_STATES) == {
        "created", "observed", "active", "swapped", "retained", "closed"}
