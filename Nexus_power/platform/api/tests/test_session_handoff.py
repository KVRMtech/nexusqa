"""Record + Run must not run when the recording produced no login.

Phase 1 of Record + Run. Chaining record -> save -> run creates a failure the
separate steps never had: the recorder ALWAYS returns a storage state, so storing it
always "succeeds" — even when it is an empty cookie jar because the operator never
completed the login. The run then starts logged out, every step fails, and the report
attributes those failures to the application under test.

That exact symptom was reported on this product before ("No session was captured, so
the crawl will start logged out"), which is why the guard is fail-closed.

Pure - no DB, no runner.
"""
from app.services.test_factory import session_handoff as sh


def _state(cookies=None, origins=None):
    out = {}
    if cookies is not None:
        out["cookies"] = cookies
    if origins is not None:
        out["origins"] = origins
    return out


def _cookie(name="sl_session"):
    return {"name": name, "value": "abc", "domain": "app.example", "path": "/"}


# ── a real session ───────────────────────────────────────────────────────────

def test_a_cookie_session_is_usable():
    v = sh.assess(_state(cookies=[_cookie()]))
    assert v["usable"] is True
    assert v["state"] == sh.USABLE
    assert v["cookies"] == 1


def test_a_token_only_single_page_app_session_is_usable():
    """SPAs often authenticate with a localStorage token and no cookie at all —
    refusing those would block a whole class of app."""
    v = sh.assess(_state(origins=[{"origin": "https://app.example",
                                   "localStorage": [{"name": "id_token", "value": "x"}]}]))
    assert v["usable"] is True
    assert v["storage_keys"] == 1
    assert v["cookies"] == 0


def test_many_cookies_are_counted_for_the_operator():
    v = sh.assess(_state(cookies=[_cookie("a"), _cookie("b"), _cookie("c")]))
    assert v["cookies"] == 3
    assert "3 cookie(s)" in v["note"]


# ── the failure this exists to stop ──────────────────────────────────────────

def test_an_EMPTY_capture_is_refused():
    """THE DEFECT. The recorder ran, the login did not. Running now would execute
    the suite logged out and blame the application for every failure."""
    v = sh.assess(_state(cookies=[], origins=[]))
    assert v["usable"] is False
    assert v["state"] == sh.EMPTY
    assert "has NOT been started" in v["note"]


def test_an_origin_with_no_stored_keys_does_not_count_as_a_session():
    """A page that set nothing still produces an origins entry. Counting the entry
    rather than its contents would let an empty session through."""
    v = sh.assess(_state(cookies=[], origins=[{"origin": "https://app.example",
                                               "localStorage": []}]))
    assert v["usable"] is False
    assert v["origins"] == 1
    assert v["storage_keys"] == 0


def test_a_nameless_storage_entry_does_not_count():
    v = sh.assess(_state(origins=[{"origin": "https://app.example",
                                   "localStorage": [{"value": "x"}]}]))
    assert v["usable"] is False


def test_a_completely_absent_state_is_refused():
    for bad in (None, "", [], 0, "not-a-state"):
        v = sh.assess(bad)
        assert v["usable"] is False
        assert v["state"] == sh.MALFORMED


def test_garbage_shapes_do_not_crash_and_are_refused():
    for bad in ({"cookies": "nope"}, {"origins": {"a": 1}}, {"cookies": None, "origins": None}):
        v = sh.assess(bad)
        assert v["usable"] is False


# ── the refusal must teach, and must never blame the app ─────────────────────

def test_the_refusal_says_what_to_do_next():
    v = sh.assess(_state(cookies=[], origins=[]))
    assert "Record the login again" in v["note"]
    assert "logged-in page" in v["note"]


def test_no_refusal_ever_implicates_the_application():
    """The app is mentioned only to say it would have been blamed WRONGLY — the
    refusal must name OUR cause (the login) as the thing that failed."""
    for bad in (None, _state(cookies=[], origins=[])):
        note = sh.assess(bad)["note"]
        assert "login" in note.lower()                 # names our own cause
        assert "has NOT been started" in note or "Nothing has" in note  # nothing ran
        # and never asserts the app misbehaved
        for blame in ("the application failed", "application error", "app is broken"):
            assert blame not in note.lower()


def test_no_cookie_VALUE_is_ever_echoed():
    """The verdict is logged and returned to the browser; a session value must not
    travel with it."""
    v = sh.assess(_state(cookies=[{"name": "sl_session", "value": "SUPERSECRET"}]))
    assert "SUPERSECRET" not in repr(v)


def test_the_judgement_cannot_false_positive_on_a_real_session():
    """The rule is deliberately crude — 'is there anything at all' — because
    guessing WHICH cookie is the auth cookie means guessing across every app in the
    world, and guessing permissively re-opens the hole this closes."""
    exotic = _state(cookies=[{"name": "__Host-x", "value": "1"}])
    assert sh.assess(exotic)["usable"] is True


# ── wired: the handoff cannot half-happen ────────────────────────────────────

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_NEXT = chr(10) + "async def "


def _handler(name: str) -> str:
    i = _ROUTER.index("async def %s(" % name)
    nxt = _ROUTER.find(_NEXT, i + 10)
    return _ROUTER[i:nxt if nxt > 0 else len(_ROUTER)]


def test_saving_a_session_reports_whether_it_can_actually_authenticate():
    """Storing always succeeds, so the endpoint must SAY what it stored — otherwise
    a caller chaining a run onto it has nothing to refuse on."""
    seg = _handler("save_auth_capture")
    assert "session_handoff.assess(state)" in seg
    assert '"session": session_verdict' in seg


def test_record_and_run_refuses_to_run_when_no_session_was_captured():
    seg = _handler("save_auth_capture_and_run")
    assert 'if not verdict.get("usable"):' in seg
    assert "status_code=422" in seg
    assert '"ran": False' in seg


def test_the_refusal_happens_BEFORE_any_run_is_dispatched():
    """A refusal that still dispatched would be the exact bug this closes."""
    seg = _handler("save_auth_capture_and_run")
    assert seg.index('if not verdict.get("usable"):') < seg.index("playwright_run_live(")


def test_record_and_run_supports_both_watch_and_headless():
    seg = _handler("save_auth_capture_and_run")
    assert "playwright_run_live(" in seg and "playwright_run(" in seg
    assert "body.watch" in seg


def test_it_reuses_the_normal_run_request_so_integrations_are_unchanged():
    """Record + Run must be the SAME run — reporting, attribution, auto-heal and
    certification all fire on the ingest path and must not need special cases."""
    assert "class _SaveAndRunBody(RunConfigRequest):" in _ROUTER


def test_the_refusal_carries_the_reason_and_the_recipe():
    seg = _handler("save_auth_capture_and_run")
    for key in ('"reason"', '"note"', '"recipe"', '"cookies"'):
        assert key in seg, key
