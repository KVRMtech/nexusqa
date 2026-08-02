"""A result you cannot attribute to an identity is weak evidence.

Phase 4 of Record + Run. The evidence report has always READ
`run.metadata_json["persona"]` for its Trust Block, and the compiled reporter has
always forwarded `NEXUS_PERSONA` — but only the HEADLESS dispatch ever set it. A
watched run recorded no identity at all, so every live run's Trust Block read
"default identity", including one deliberately run as a named member.

`persona` alone also cannot separate a login recorded moments ago (Record + Run)
from the artifact's long-stored session. Those have very different shelf lives, so a
reader cannot judge what the result is worth. Hence a separate `identity` label.
"""
from app.routers.test_factory import _identity_label


class _Body:
    def __init__(self, identity=""):
        self.identity = identity


# ── the label ────────────────────────────────────────────────────────────────

def test_a_named_member_is_labelled_member():
    assert _identity_label(_Body(), "p-123") == "member"


def test_record_and_run_is_labelled_recorded_session():
    assert _identity_label(_Body("recorded_session"), "") == "recorded_session"


def test_an_ordinary_run_is_labelled_stored_session():
    assert _identity_label(_Body(), "") == "stored_session"


def test_a_member_outranks_a_recorded_session():
    """If both are present the member IS the identity — the card is what actually
    authenticated."""
    assert _identity_label(_Body("recorded_session"), "p-123") == "member"


def test_a_caller_cannot_forge_an_identity():
    """Only the literal 'recorded_session' is honoured from the request; anything
    else is derived. A run must not be able to describe itself as an identity it
    does not have."""
    for forged in ("member", "certified", "admin", "trusted", "MEMBER", " member "):
        assert _identity_label(_Body(forged), "") == "stored_session"


def test_a_body_without_the_field_does_not_crash():
    class Bare:
        pass
    assert _identity_label(Bare(), "") == "stored_session"


# ── wired on BOTH dispatch paths ─────────────────────────────────────────────

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_NEXT = chr(10) + "async def "


def _handler(name):
    i = _ROUTER.index("async def %s(" % name)
    nxt = _ROUTER.find(_NEXT, i + 10)
    return _ROUTER[i:nxt if nxt > 0 else len(_ROUTER)]


def test_both_dispatch_paths_record_the_identity():
    for name in ("playwright_run", "playwright_run_live"):
        seg = _handler(name)
        assert '"NEXUS_IDENTITY": _identity_label(body, persona_id)' in seg, name


def test_the_live_run_records_the_member_and_the_posture_it_ran_under():
    """It recorded neither. The posture is the governance floor the result was
    produced under — without it the Trust Block cannot say what was permitted."""
    seg = _handler("playwright_run_live")
    assert '"NEXUS_PERSONA": persona_id' in seg
    assert "NEXUS_POSTURE" in seg


def test_record_and_run_declares_itself_server_side():
    seg = _handler("save_auth_capture_and_run")
    assert 'body.identity = "recorded_session"' in seg


def test_the_reporter_forwards_identity_to_the_ingest():
    """The chain is env var -> reporter -> ingest metadata -> run row -> Trust Block.
    A break anywhere silently empties it."""
    comp = open("app/services/script_factory/compiler.py", encoding="utf-8").read()
    assert "identity: process.env.NEXUS_IDENTITY || ''" in comp


def test_the_report_surfaces_it():
    rep = open("app/services/test_factory/evidence_report.py", encoding="utf-8").read()
    assert '"identity": str((getattr(run, "metadata_json", None) or {}).get("identity") or "")' in rep
