"""M1.5 — the page lifecycle, proven against REAL Chromium (T-ND-01 … T-ND-05).

Every test here drives :class:`app.playwright_port.PlaywrightBrowserPort` — the
same class ``app.main._run_job`` constructs for a live crawl — against a fixture
served over HTTP.  Nothing between the fixture HTML and the assertion is a test
double: the dialog is a real ``confirm()``, the popup is a real second page in a
real browser context, and the download artifact is read back off the filesystem.

WHY IT HAS TO BE THIS LANE.  Every behaviour in M1.5 is invisible to the DOM.
Capture of fixture 19 is byte-identical before and after the dialog fix, because
a native dialog is not in the document; capture of fixture 21 cannot tell you
which of two pages the crawler is standing on.  A unit test with a scripted fake
would prove the policy (that is
``tests/test_page_lifecycle.py``) and nothing about whether the policy is ever
reached.  These tests close that half.

Each test takes its OWN browser context.  M1.5 is the first behaviour that
mutates which page is active, so sharing the session context would make each
test's result depend on which ran before it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import _harness as H

pytestmark = [pytest.mark.browser, pytest.mark.playwright]

CONFIRM_FIXTURE = "19-native-confirm-dialog"
DOWNLOAD_FIXTURE = "20-download-artifact"
POPUP_FIXTURE = "21-new-tab-adoption"


# ─── Driving the production port ─────────────────────────────────────────────

def _in_scope_127(url: str) -> bool:
    """The crawl's scope test, reduced to what these fixtures need.

    ``FixtureServer`` serves the SAME tree from two genuinely different origins
    (``127.0.0.1`` and ``localhost``), so "in scope" here is exactly the
    distinction fixture 21's partner popup depends on.
    """
    return "//127.0.0.1:" in (url or "")


class _Session:
    """One context + one page + the production port, plus the journey context a
    crawler would have bound."""

    def __init__(self, pw: Any, artifact_dir: Path, *, observe_only: bool = False,
                 phase: str = "walk", approvals: tuple[str, ...] = ()) -> None:
        from app.playwright_port import PlaywrightBrowserPort

        self._pw = pw
        self.artifact_dir = artifact_dir
        self.context = pw.run(pw.fresh_context())
        self.page = pw.run(self.context.new_page())
        self.port = PlaywrightBrowserPort(
            self.page, self.context, artifact_dir=str(artifact_dir))
        self.port.bind_journey_context(lambda: {
            "phase": phase, "observe_only": observe_only,
            "approved_labels": approvals})
        self.port.bind_scope_check(_in_scope_127)

    def run(self, coro: Any) -> Any:
        return self._pw.run(coro)

    def close(self) -> None:
        try:
            self._pw.run(self.context.close())
        except Exception:
            pass


@pytest.fixture
def session(pw, tmp_path):
    """A fresh context/page/port per test, torn down whatever the outcome."""
    made: list[_Session] = []

    def _make(**kwargs: Any) -> _Session:
        s = _Session(pw, tmp_path / f"artifacts{len(made)}", **kwargs)
        made.append(s)
        return s

    yield _make
    for s in made:
        s.close()


def _control(controls: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for c in controls:
        if str(c.get("name") or "").strip() == name:
            return c
    raise AssertionError(f"control {name!r} not captured; got "
                         f"{sorted(str(c.get('name') or '') for c in controls)}")


def _events(events: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [e for e in events if e.get("event") == kind]


# ─── The wiring itself ───────────────────────────────────────────────────────

def test_the_production_context_accepts_downloads() -> None:
    """T-ND-03's precondition, asserted on the BROWSER LAYER's declaration and
    on the entrypoint that reads it — not on a value a test re-typed."""
    import inspect

    from app.main import _run_job
    from app.playwright_port import context_defaults

    assert context_defaults()["accept_downloads"] is True
    src = inspect.getsource(_run_job)
    assert "context_defaults()" in src, (
        "app.main no longer reads the browser layer's context declaration — "
        "accept_downloads is not guaranteed for a live crawl")


def test_the_port_starts_with_exactly_one_active_page(session) -> None:
    """One ACTIVE page and one only — the invariant every later assertion rests
    on, and the thing "which page is authoritative" used to be ambiguous about."""
    from app import page_lifecycle as pl

    s = session()
    assert s.port.registry.active_token() == "", (
        "the primary page must carry the EMPTY token, or every fingerprint ever "
        "recorded re-keys on the next crawl")
    assert s.port.registry.open_count() == 1
    snapshot = s.port.registry.snapshot()
    assert [r["lifecycle"] for r in snapshot] == [pl.LIFECYCLE_ACTIVE]


def test_listeners_deferred_by_a_missing_event_loop_are_attached_later(
        pw, fixture_server, session) -> None:
    """A DEFECT M1.5 FOUND, not one it introduced.

    Playwright subscribes to ``response`` and ``dialog`` by sending a protocol
    message, so ``page.on()`` for those two needs a RUNNING EVENT LOOP — and the
    adapter's ``__init__`` is synchronous.  Built outside a coroutine (which is
    how the characterization lane and this test build it) both used to raise, get
    logged, and be abandoned: every crawl in that lane captured zero network
    evidence and said so only in a warning nobody read.

    The port now queues the failures and re-attaches at the first async call.
    Proven by OUTCOME — network evidence arrives, and a dialog is answered —
    not by inspecting a listener table.
    """
    s = session()                      # constructed outside the loop, on purpose
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        # A dialog is answered ⇒ the deferred `dialog` subscription took.
        await s.port.click(_control(await s.port.collect_controls(), "Continue"))
        return await s.port.drain_browser_events()

    events = s.run(_run())
    assert _events(events, "dialog"), (
        "the deferred dialog listener never attached — Playwright would have "
        "auto-dismissed and the funnel would silently not advance")


# ─── T-ND-02 · native dialogs ────────────────────────────────────────────────

def test_a_confirm_gated_continue_ADVANCES(pw, fixture_server, session) -> None:
    """THE headline acceptance criterion.

    Before M1.5 Playwright auto-dismissed this confirm, ``location.href`` never
    ran, and the click classified as outcome ``none`` — indistinguishable from a
    dead button.  The journey must now reach step 2.
    """
    s = session()
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        observation = await s.port.click(_control(controls, "Continue"))
        return observation, await s.port.current_url(), await s.port.drain_browser_events()

    observation, landed, events = s.run(_run())

    assert landed.endswith("/step2.html"), (
        f"the confirm-gated Continue did not advance; landed on {landed}")
    assert observation.url_after.endswith("/step2.html")
    assert observation.intent_met is not False

    dialogs = _events(events, "dialog")
    assert len(dialogs) == 1, f"expected one recorded dialog, got {dialogs}"
    record = dialogs[0]
    assert record["dialog_type"] == "confirm"
    assert record["action"] == "accept"
    assert record["intent"] == "funnel_confirmation"
    assert record["control_label"] == "Continue", (
        "the dialog was not attributed to the control that raised it")
    assert record["action_verb"] == "click"
    assert record["journey_phase"] == "walk"
    assert "want to continue" in record["message"]
    assert record["reason"], "a decision with no recorded reason is not auditable"
    assert record["handled"] is True and not record["error"]


def test_a_leave_warning_does_NOT_abandon_the_journey(pw, fixture_server, session) -> None:
    """The opposite failure to auto-dismissal, and just as destructive: a crawler
    that accepts every dialog leaves for the dashboard and the funnel is gone."""
    s = session()
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Return to dashboard"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    landed, events = s.run(_run())

    assert landed.endswith("/index.html"), (
        f"a leave warning was accepted and the journey was abandoned to {landed}")
    record = _events(events, "dialog")[0]
    assert record["action"] == "dismiss"
    assert record["intent"] == "leave_warning"
    assert "leave this page" in record["message"].lower()


def test_a_destructive_confirm_is_refused_without_an_approval(
        pw, fixture_server, session) -> None:
    """A native ``confirm()`` is not an approved crossing."""
    s = session()
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Delete Application"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    landed, events = s.run(_run())

    assert not landed.endswith("/deleted.html"), "the application was deleted"
    record = _events(events, "dialog")[0]
    assert record["action"] == "dismiss"
    assert record["intent"] == "destructive_confirmation"


def test_a_destructive_confirm_IS_accepted_under_an_operator_approval(
        pw, fixture_server, session) -> None:
    """The approval subsystem, not the dialog text, is the authority — proven by
    flipping only the grant and watching the landing change."""
    s = session(approvals=("Delete Application",))
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Delete Application"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    landed, events = s.run(_run())

    assert landed.endswith("/deleted.html")
    record = _events(events, "dialog")[0]
    assert record["action"] == "accept"
    assert "operator approval" in record["reason"]


def test_an_alert_is_acknowledged_and_a_prompt_is_declined(
        pw, fixture_server, session) -> None:
    """Both BLOCK the page until answered; neither may be left hanging."""
    s = session()
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Show session notice"))
        await s.port.click(_control(controls, "Add a reference code"))
        statuses = await s.port.status_texts()
        return statuses, await s.port.drain_browser_events()

    statuses, events = s.run(_run())
    by_type = {e["dialog_type"]: e for e in _events(events, "dialog")}

    assert by_type["alert"]["action"] == "accept"
    assert by_type["alert"]["intent"] == "notice"
    assert by_type["prompt"]["action"] == "dismiss"
    assert by_type["prompt"]["intent"] == "prompt_unanswerable"
    # The page ran on afterwards — proof the dialogs were actually answered and
    # not merely observed (an unanswered dialog blocks the page forever).
    assert any("No reference supplied" in t for t in statuses), statuses


def test_observe_only_dismisses_a_funnel_confirm(pw, fixture_server, session) -> None:
    """Posture raises the floor: an observe-only crawl may not commit a mutation
    a confirm is gating, so the same click that advances above must not here."""
    s = session(observe_only=True)
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Continue"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    landed, events = s.run(_run())
    assert landed.endswith("/index.html")
    assert _events(events, "dialog")[0]["action"] == "dismiss"


def test_a_control_that_raises_no_dialog_records_no_dialog(
        pw, fixture_server, session) -> None:
    """The dialog path must not be taken indiscriminately."""
    s = session()
    url = fixture_server.url(CONFIRM_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Print summary"))
        return await s.port.drain_browser_events()

    assert _events(s.run(_run()), "dialog") == []


# ─── T-ND-03 · downloads ─────────────────────────────────────────────────────

def test_a_download_produces_a_REAL_FILE(pw, fixture_server, session) -> None:
    """Not a log line.  The bytes are read back off disk and checked."""
    s = session()
    url = fixture_server.url(DOWNLOAD_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Download Sales Packet"))
        return await s.port.drain_browser_events()

    events = s.run(_run())
    downloads = _events(events, "download")
    assert len(downloads) == 1, f"expected one download record, got {downloads}"
    record = downloads[0]

    assert record["captured"] is True, f"nothing was captured: {record}"
    assert record["filename"] == "sales-packet.pdf"
    assert record["content_type"] == "application/pdf"
    assert record["trigger_label"] == "Download Sales Packet"
    assert record["page_url"].endswith("/20-download-artifact/index.html")
    assert record["artifact_path"].startswith("artifacts/")

    on_disk = s.artifact_dir / Path(record["artifact_path"]).name
    assert on_disk.exists(), f"the manifest claims {record['artifact_path']}, no file there"
    data = on_disk.read_bytes()
    assert len(data) > 0, "a zero-byte artifact is not evidence"
    assert record["bytes"] == len(data)
    assert data.startswith(b"%PDF-"), "the captured artifact is not the PDF"
    assert data.rstrip().endswith(b"%%EOF"), "the artifact is truncated"


def test_a_client_generated_download_is_captured_too(
        pw, fixture_server, session) -> None:
    """An "Export to CSV" button: no href, no server file.  Capture sees a plain
    <button> and cannot tell a download is coming — only the browser can."""
    s = session()
    url = fixture_server.url(DOWNLOAD_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Export Policy Schedule"))
        return await s.port.drain_browser_events()

    record = _events(s.run(_run()), "download")[0]
    assert record["captured"] is True
    assert record["filename"] == "policy-schedule.csv"
    assert record["content_type"] == "text/csv"
    body = (s.artifact_dir / Path(record["artifact_path"]).name).read_bytes()
    assert b"policy,holder,face_amount" in body


def test_a_hostile_suggested_filename_stays_inside_the_artifact_directory(
        pw, fixture_server, session) -> None:
    """``suggested_filename`` is application-controlled text."""
    s = session()
    url = fixture_server.url(DOWNLOAD_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Export Audit Log"))
        return await s.port.drain_browser_events()

    record = _events(s.run(_run()), "download")[0]
    assert record["captured"] is True
    stored = Path(record["artifact_path"]).name
    assert "/" not in stored and ".." not in stored.replace("...", "")
    written = s.artifact_dir / stored
    assert written.exists() and written.parent == s.artifact_dir, (
        f"the artifact escaped its directory: {written}")
    assert b"audit entry 1" in written.read_bytes()


def test_an_ordinary_link_is_not_recorded_as_a_download(
        pw, fixture_server, session) -> None:
    s = session()
    url = fixture_server.url(DOWNLOAD_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Claims"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    landed, events = s.run(_run())
    assert landed.endswith("/claims.html")
    assert _events(events, "download") == []


# ─── T-ND-01 · popup / new-tab adoption ──────────────────────────────────────

def test_a_target_blank_step_is_FOLLOWED(pw, fixture_server, session) -> None:
    """The port must end up ACTING AGAINST the new tab, not the opener."""
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Details"))
        active = await s.port.current_url()
        token = await s.port.active_page_token()
        title = await s.port.title()
        adopted_controls = await s.port.collect_controls()
        return active, token, title, adopted_controls, await s.port.drain_browser_events()

    active, token, title, adopted_controls, events = s.run(_run())

    assert active.endswith("/details.html"), (
        f"the crawler is still standing on the opener ({active})")
    assert token == "p1", "an adopted page must carry a non-primary identity"
    assert "quote details" in title.lower()
    # INVENTORIED as part of the journey: the controls now read are the new
    # tab's, which is what T-ND-01 asks for.
    names = {c["name"] for c in adopted_controls}
    assert {"Accept Quote", "Decline Quote", "Coverage Note"} <= names, names

    popups = _events(events, "popup")
    assert len(popups) == 1
    record = popups[0]
    assert record["adopted"] is True and record["disposition"] == "adopt"
    assert record["popup_url"].endswith("/details.html")
    assert record["opener_url"].endswith("/21-new-tab-adoption/index.html")
    assert record["page_token"] == "p1"
    assert record["trigger_label"] == "Open Details"
    assert record["reason"]


def test_the_opener_stays_open_after_adoption(pw, fixture_server, session) -> None:
    """The original page is RETAINED, not closed — and it is not authoritative."""
    from app import page_lifecycle as pl

    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Details"))
        return len(s.context.pages), s.port.registry.snapshot()

    page_count, snapshot = s.run(_run())
    assert page_count == 2, "the opener was closed"
    by_token = {row["token"]: row for row in snapshot}
    assert by_token["primary"]["lifecycle"] == pl.LIFECYCLE_SWAPPED
    assert by_token["p1"]["lifecycle"] == pl.LIFECYCLE_ACTIVE
    assert sum(1 for r in snapshot if r["lifecycle"] == pl.LIFECYCLE_ACTIVE) == 1


def test_window_open_with_a_url_is_adopted(pw, fixture_server, session) -> None:
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Underwriting Window"))
        return await s.port.current_url(), await s.port.collect_controls()

    active, controls = s.run(_run())
    assert active.endswith("/underwriting.html")
    assert "Underwriting Decision" in {c["name"] for c in controls}


def test_a_popup_that_navigates_AFTER_creation_is_adopted_on_where_it_lands(
        pw, fixture_server, session) -> None:
    """``window.open('')`` then a deferred ``location.href``.

    Judged at creation the page is ``about:blank``, so a crawler that decides
    immediately concludes "never navigated" and drops the real journey.  The
    adapter waits on Playwright's own URL predicate — not a sleep.
    """
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Deferred Window"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    active, events = s.run(_run())
    assert active.endswith("/deferred.html"), (
        f"the deferred popup was not followed to its landing ({active})")
    record = _events(events, "popup")[0]
    assert record["adopted"] is True
    assert "about:blank" not in record["popup_url"]


def test_a_foreign_origin_popup_is_recorded_but_never_adopted(
        pw, fixture_server, session) -> None:
    """Attributing a partner site to the application under test is the same
    error as inventorying an identity provider as the app."""
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Partner Site"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    active, events = s.run(_run())
    assert active.endswith("/21-new-tab-adoption/index.html"), (
        "a foreign-origin popup became the journey")
    record = _events(events, "popup")[0]
    assert record["adopted"] is False
    assert record["disposition"] == "retain"
    assert "out_of_scope" in record["reason"]
    assert "partner.html" in record["popup_url"], "the popup is still RECORDED"


def test_a_self_closing_popup_is_recorded_and_never_adopted(
        pw, fixture_server, session) -> None:
    """Adopting a dead handle would break every subsequent action."""
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Transient Window"))
        # The port must still be usable afterwards — that is the real assertion.
        return (await s.port.current_url(), await s.port.collect_controls(),
                await s.port.drain_browser_events())

    active, controls, events = s.run(_run())
    assert active.endswith("/21-new-tab-adoption/index.html")
    assert "Open Details" in {c["name"] for c in controls}, (
        "the port stopped working after a transient popup")
    popups = _events(events, "popup")
    assert popups and popups[0]["adopted"] is False


def test_when_the_active_page_closes_an_open_page_is_promoted(
        pw, fixture_server, session) -> None:
    """PAGE REPLACEMENT.  Somebody has to inherit the journey or every later
    action fails against a dead target."""
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Details"))
        assert (await s.port.current_url()).endswith("/details.html")
        await s.port.drain_browser_events()          # clear the adoption record
        # The application closes the tab the journey moved into.
        await s.port._page.close()
        # Any port call reaches the synchronisation point and re-homes the walk.
        recovered = await s.port.goto(url)
        return (recovered, await s.port.current_url(),
                await s.port.active_page_token(), await s.port.drain_browser_events())

    recovered, active, token, events = s.run(_run())
    assert recovered.ok, f"the crawl could not recover from a closed page: {recovered}"
    assert active.endswith("/21-new-tab-adoption/index.html")
    assert token == "", "the primary page should have inherited the journey"
    closes = _events(events, "page_closed")
    assert closes, f"a page left the journey and nothing recorded it: {events}"
    assert closes[0]["was_active"] is True
    assert closes[0]["promoted_token"] == ""


def test_multiple_popups_from_one_action_adopt_exactly_one(
        pw, fixture_server, session) -> None:
    """ONE click, TWO windows.

    Which one becomes the journey has to be decided deterministically — "it
    picked a tab" is not an answer anybody can audit — and the one that does not
    win still has to be recorded, with the reason it lost.
    """
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        await s.port.click(_control(await s.port.collect_controls(),
                                    "Open Two Windows"))
        return await s.port.current_url(), await s.port.drain_browser_events()

    active, events = s.run(_run())
    popups = _events(events, "popup")
    adopted = [p for p in popups if p["adopted"]]

    assert len(popups) == 2, f"both popups must be recorded, got {popups}"
    assert len(adopted) == 1, f"exactly one adoption expected, got {adopted}"
    # FIRST usable in-scope popup wins — the one the click produced first.
    assert adopted[0]["popup_url"].endswith("/details.html"), adopted[0]
    loser = [p for p in popups if not p["adopted"]][0]
    assert "superseded" in loser["reason"], loser
    assert loser["popup_url"].endswith("/underwriting.html")
    assert active.endswith("/details.html")


def test_a_second_popup_opened_LATER_may_still_take_over(
        pw, fixture_server, session) -> None:
    """The counterpart to the rule above, so its scope is unambiguous.

    "First wins" governs one BATCH — popups the same action produced.  A popup
    opened by a later action is a later move of the journey and is adopted on
    its own merits; otherwise the walk would be stuck on whichever tab it
    happened to enter first.
    """
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        controls = await s.port.collect_controls()
        await s.port.click(_control(controls, "Open Details"))
        first = await s.port.current_url()
        # Back to the opener, then a SECOND, separate action.
        await s.port.goto(url)
        await s.port.click(_control(await s.port.collect_controls(),
                                    "Open Underwriting Window"))
        return first, await s.port.current_url()

    first, second = s.run(_run())
    assert first.endswith("/details.html")
    assert second.endswith("/underwriting.html")


# ─── T-ND-04 · state identity follows the adopted page ───────────────────────

def test_identity_follows_the_adopted_page(pw, fixture_server, session) -> None:
    """The full production identity path: observe → inventory → fingerprint.

    Uses the same three calls ``discovery._expand`` makes, so this is the real
    identity computation and not a re-implementation of it.
    """
    from app.inventory import build_inventory
    from app.guard import load_refuse_pack
    from app.state_identity import StateFingerprinter

    pack = load_refuse_pack(str(Path(H.SERVICE_ROOT) / "app" / "refuse_pack.yaml"))
    fingerprinter = StateFingerprinter()
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _identity():
        page_url = await s.port.current_url()
        controls = build_inventory(await s.port.collect_controls(), pack, url=page_url)
        return fingerprinter.fingerprint(
            url=page_url, controls=controls,
            dialogs=await s.port.dialog_flags(),
            page_token=await s.port.active_page_token()), page_url

    async def _run():
        await s.port.goto(url)
        before, url_before = await _identity()
        await s.port.click(_control(await s.port.collect_controls(), "Open Details"))
        after, url_after = await _identity()
        return before, url_before, after, url_after

    before, url_before, after, url_after = s.run(_run())

    assert url_before.endswith("/index.html") and url_after.endswith("/details.html")
    assert before != after, "the adopted page inherited the stale page's identity"


def test_a_popup_IDENTICAL_to_its_opener_still_gets_a_distinct_identity(
        pw, fixture_server, session) -> None:
    """THE T-ND-04 CASE, live.

    ``window.open(location.href)`` — the popup's URL template, interactive
    controls and dialog flags are all identical to the opener's, so every signal
    the base fingerprint reads collapses.  Without the page identity the popup
    would silently BE the opener.
    """
    from app.inventory import build_inventory
    from app.guard import load_refuse_pack
    from app.state_identity import StateFingerprinter

    pack = load_refuse_pack(str(Path(H.SERVICE_ROOT) / "app" / "refuse_pack.yaml"))
    fingerprinter = StateFingerprinter()
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _read():
        page_url = await s.port.current_url()
        controls = build_inventory(await s.port.collect_controls(), pack, url=page_url)
        dialogs = await s.port.dialog_flags()
        token = await s.port.active_page_token()
        return page_url, controls, dialogs, token

    async def _run():
        await s.port.goto(url)
        before = await _read()
        await s.port.click(_control(await s.port.collect_controls(),
                                    "Open Identical Copy"))
        after = await _read()
        return before, after

    before, after = s.run(_run())
    (url_b, controls_b, dialogs_b, token_b) = before
    (url_a, controls_a, dialogs_a, token_a) = after

    # The premise: the two pages really are indistinguishable to the DOM.
    assert url_b == url_a, "the fixture no longer opens an identical URL"
    assert ({(c["role"], c["name"]) for c in controls_b}
            == {(c["role"], c["name"]) for c in controls_a}), (
        "the fixture no longer renders identical controls")
    assert token_b == "" and token_a == "p1", (token_b, token_a)

    fp_b = fingerprinter.fingerprint(url=url_b, controls=controls_b,
                                     dialogs=dialogs_b, page_token=token_b)
    fp_a = fingerprinter.fingerprint(url=url_a, controls=controls_a,
                                     dialogs=dialogs_a, page_token=token_a)
    assert fp_b != fp_a, (
        "a DIFFERENT page produced the SAME state identity — the crawler would "
        "de-duplicate the adopted tab as the page it had already left")


def test_no_stale_page_actions_after_adoption(pw, fixture_server, session) -> None:
    """Every subsequent verb must land on the adopted page.

    Asserted by ACTING: a control that exists only on the new tab is filled, and
    the value is read back from it.  A port still pointing at the opener could
    not resolve the locator at all.
    """
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        await s.port.click(_control(await s.port.collect_controls(), "Open Details"))
        adopted = await s.port.collect_controls()
        observation = await s.port.fill(_control(adopted, "Coverage Note"),
                                        "term life, 20 years")
        shot = await s.port.screenshot_png()
        return observation, await s.port.current_url(), len(shot)

    observation, active, shot_bytes = s.run(_run())
    assert observation.committed_value == "term life, 20 years"
    assert observation.intent_met is True
    assert active.endswith("/details.html")
    assert shot_bytes > 0, "the evidence screenshot came from no page at all"


def test_ordinary_same_tab_navigation_is_unchanged(pw, fixture_server, session) -> None:
    """The regression guard for the common path: no page event, no adoption, no
    token, and not one browser event recorded."""
    s = session()
    url = fixture_server.url(POPUP_FIXTURE)

    async def _run():
        await s.port.goto(url)
        await s.port.click(_control(await s.port.collect_controls(),
                                    "View Details In Place"))
        return (await s.port.current_url(), await s.port.active_page_token(),
                len(s.context.pages), await s.port.drain_browser_events())

    active, token, pages, events = s.run(_run())
    assert active.endswith("/details.html")
    assert token == "", "same-tab navigation must not mint a page identity"
    assert pages == 1
    assert events == [], f"same-tab navigation recorded browser events: {events}"


# ─── T-ND-05 · the whole thing, through the real Crawler ─────────────────────

def _crawl(pw, url: str, work_dir: Path, *, crawl_id: str,
           max_states: int = 6, max_actions: int = 12) -> list[dict[str, Any]]:
    """Drive the PRODUCTION Crawler over ``url`` and return its manifest records.

    Mirrors ``app.main._run_job``'s construction the same way
    ``test_browser_characterization._run_real_crawl`` does, but with a budget
    that permits ACTIONS — a crawl that never clicks can never open a popup.
    """
    from app.auth import AuthWindow
    from app.crawler import Budget, Crawler, GuardContext
    from app.guard import load_refuse_pack
    from app.main import EXPLORER_VERSION, PlaywrightBrowserPort
    from app.playwright_port import context_defaults

    pack = load_refuse_pack(str(Path(H.SERVICE_ROOT) / "app" / "refuse_pack.yaml"))
    guard_ctx = GuardContext(
        refuse_pack=pack,
        auth_window=AuthWindow(max_requests=50, window_ms=60_000),
        attestation=None, submit_flow_approved=False, idp_domains=frozenset(),
    )
    budget = Budget.from_dict({
        "max_states": max_states, "max_actions": max_actions,
        "max_requests": 400, "max_duration_ms": 180_000,
    })

    async def _boot():
        ctx = await pw.fresh_context()
        page = await ctx.new_page()
        return ctx, page

    context, page = pw.run(_boot())
    try:
        assert context_defaults()["accept_downloads"] is True
        port = PlaywrightBrowserPort(
            page, context,
            artifact_dir=str(Path(work_dir) / crawl_id / "artifacts"))
        crawler = Crawler(
            port, crawl_id=crawl_id, tenant_id="m15",
            target_url=url, work_dir=str(work_dir), refuse_pack=pack,
            budget=budget, explorer_version=EXPLORER_VERSION,
            guard_version=EXPLORER_VERSION, refuse_pack_version=pack.version,
            config_fingerprint="m15-fixed", guard_context=guard_ctx,
            identity_seed="qec-m15", observe_only=False,
        )
        pw.run(crawler.run())
    finally:
        try:
            pw.run(context.close())
        except Exception:
            pass

    manifest = Path(work_dir) / crawl_id / "manifest.jsonl"
    assert manifest.exists(), f"no manifest written at {manifest}"
    return [json.loads(line) for line in
            manifest.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_a_real_crawl_inventories_the_adopted_tab(pw, fixture_server, tmp_path) -> None:
    """T-ND-01's acceptance, end to end through the real state machine.

    The crawler's own click pass opens the popups; the port adopts one; the
    manifest must then contain a ``page_state`` for the ADOPTED page and a
    ``browser_event`` explaining how the crawl got there.
    """
    records = _crawl(pw, fixture_server.url(POPUP_FIXTURE), tmp_path,
                     crawl_id="m15-popup")

    browser_events = [r for r in records if r.get("type") == "browser_event"]
    popups = [r for r in browser_events if r.get("event") == "popup"]
    assert popups, f"no popup evidence in a crawl of the popup fixture: {browser_events}"
    adopted = [p for p in popups if p.get("adopted")]
    assert adopted, f"no popup was ever adopted: {popups}"

    states = [r for r in records if r.get("type") == "page_state"]
    # ``page_state`` names the page ``location`` (it mirrors schema.PageState),
    # not ``url`` — reading the wrong key would make this assertion vacuous.
    urls = {str(r.get("location") or "") for r in states}
    adopted_urls = {p["popup_url"] for p in adopted}
    assert any(any(u.endswith(Path(a).name) for a in adopted_urls) for u in urls), (
        f"the adopted tab was never inventoried; states={sorted(urls)} "
        f"adopted={sorted(adopted_urls)}")

    # And the retained ones are in the record too, with their reasons.
    retained = [p for p in popups if not p.get("adopted")]
    assert all(p.get("reason") for p in retained), retained


def test_a_real_crawl_answers_dialogs_and_records_every_decision(
        pw, fixture_server, tmp_path) -> None:
    """T-ND-02 end to end, and the honest counter-check: the destructive landing
    must NOT appear among the states the crawl recorded."""
    records = _crawl(pw, fixture_server.url(CONFIRM_FIXTURE), tmp_path,
                     crawl_id="m15-dialog")

    dialogs = [r for r in records
               if r.get("type") == "browser_event" and r.get("event") == "dialog"]
    assert dialogs, "a crawl of the dialog fixture recorded no dialog decisions"
    assert all(d.get("action") in ("accept", "dismiss") for d in dialogs), dialogs
    assert all(d.get("reason") for d in dialogs), "an unexplained decision"

    intents = {d["intent"] for d in dialogs}
    assert "funnel_confirmation" in intents, intents

    state_urls = {str(r.get("location") or "") for r in records
                  if r.get("type") == "page_state"}
    assert any(u.endswith("/step2.html") for u in state_urls), (
        f"the confirm-gated step was never reached: {sorted(state_urls)}")
    assert not any(u.endswith("/deleted.html") for u in state_urls), (
        "the crawl accepted a destructive native confirm with no approval")


def test_a_real_crawl_captures_a_download_artifact(pw, fixture_server, tmp_path) -> None:
    """T-ND-03 end to end: the file lands beside the manifest that names it."""
    records = _crawl(pw, fixture_server.url(DOWNLOAD_FIXTURE), tmp_path,
                     crawl_id="m15-download")

    downloads = [r for r in records
                 if r.get("type") == "browser_event" and r.get("event") == "download"]
    assert downloads, "a crawl of the download fixture captured nothing"
    captured = [d for d in downloads if d.get("captured")]
    assert captured, f"downloads were seen but no artifact was written: {downloads}"

    crawl_dir = tmp_path / "m15-download"
    for record in captured:
        artifact = crawl_dir / record["artifact_path"]
        assert artifact.exists(), (
            f"the manifest names {record['artifact_path']} and no file is there")
        assert artifact.stat().st_size == record["bytes"] > 0
        assert artifact.parent == crawl_dir / "artifacts"


def test_browser_events_reach_the_manifest_through_the_real_emitter(
        pw, fixture_server, tmp_path) -> None:
    """The evidence must be schema-shaped and durable, not just in memory."""
    records = _crawl(pw, fixture_server.url(POPUP_FIXTURE), tmp_path,
                     crawl_id="m15-schema")
    events = [r for r in records if r.get("type") == "browser_event"]
    assert events
    for event in events:
        assert event.get("event"), f"a browser_event with no discriminator: {event}"
        assert isinstance(event.get("timestamp_ms"), int)
        assert "page_token" in event or event["event"] == "buffer_truncated"
