"""A run must be as a real, current member of THIS application.

F5. `persona_id` is a declared input that dispatch never checked, so three things
happened silently: a retired member kept running from a pinned CI config, a member
of a different application in the same tenant was accepted, and an id resolving to
nothing ran anyway while the report named a member nobody defined.

The refusal belongs at dispatch, not in the picker — the picker already hides
retired members, and every one of these arrives from callers that never see it.

Pure - no DB, no live stack.
"""
import pytest

from app.services.test_factory import persona_identity as pi

ART = "art-1"


def _p(**kw):
    base = {"persona_id": "p-1", "artifact_id": ART, "name": "Member A",
            "status": "active", "traits": [], "behavior_class": ""}
    base.update(kw)
    return base


# ── the ordinary case ────────────────────────────────────────────────────────

def test_an_active_member_of_this_application_is_allowed():
    v = pi.check_persona(persona_id="p-1", persona=_p(), artifact_id=ART)
    assert v["allowed"] is True
    assert v["reason"] == ""


def test_no_member_declared_is_unchanged_behaviour():
    """The form-login path. Nothing is claimed about an identity, so there is
    nothing to verify — and adding a refusal here would break every existing run
    that does not name a member."""
    for empty in ("", "   ", None):
        assert pi.check_persona(persona_id=empty, persona=None,
                                artifact_id=ART)["allowed"] is True


# ── retired ──────────────────────────────────────────────────────────────────

def test_a_RETIRED_member_is_refused():
    """THE DEFECT. retire() hides the member from the picker; a CI job with the id
    pinned keeps authenticating as a decommissioned account. The screen says the
    member is gone, the pipeline says otherwise, and the pipeline is the one
    producing the evidence."""
    v = pi.check_persona(persona_id="p-1", persona=_p(status="retired"), artifact_id=ART)
    assert v["allowed"] is False
    assert v["reason"] == "persona_retired"
    assert "decommissioned" in v["detail"]["note"]


def test_any_non_active_status_is_refused_not_just_the_word_retired():
    for status in ("retired", "disabled", "locked", "suspended", "RETIRED"):
        v = pi.check_persona(persona_id="p-1", persona=_p(status=status), artifact_id=ART)
        assert v["allowed"] is False, status


def test_a_missing_status_defaults_to_active():
    """A row written before the column existed must not be refused."""
    p = _p()
    p.pop("status")
    assert pi.check_persona(persona_id="p-1", persona=p, artifact_id=ART)["allowed"] is True


# ── foreign artifact ─────────────────────────────────────────────────────────

def test_a_member_of_ANOTHER_application_is_refused():
    """get_persona filters by tenant only, so any persona_id in the tenant resolves.
    Its card is then used against this artifact's login."""
    v = pi.check_persona(persona_id="p-1", persona=_p(artifact_id="art-OTHER"),
                         artifact_id=ART)
    assert v["allowed"] is False
    assert v["reason"] == "persona_belongs_to_another_application"
    assert v["detail"]["persona_artifact_id"] == "art-OTHER"


def test_the_foreign_refusal_names_both_applications():
    v = pi.check_persona(persona_id="p-1", persona=_p(artifact_id="art-OTHER"),
                         artifact_id=ART)
    assert v["detail"]["artifact_id"] == ART
    assert v["detail"]["persona_artifact_id"] == "art-OTHER"


def test_a_row_with_no_artifact_recorded_is_not_refused_as_foreign():
    """Absence of information is not evidence of a mismatch — refusing here would
    block rows written before the column was populated."""
    assert pi.check_persona(persona_id="p-1", persona=_p(artifact_id=""),
                            artifact_id=ART)["allowed"] is True


# ── does not exist ───────────────────────────────────────────────────────────

def test_an_id_that_resolves_to_NOTHING_is_refused():
    """Otherwise the run proceeds down the form-login path and reports the
    persona_id it was handed — a member that does not exist."""
    v = pi.check_persona(persona_id="p-ghost", persona=None, artifact_id=ART)
    assert v["allowed"] is False
    assert v["reason"] == "persona_not_registered"


# ── persona-0, the synthetic legacy member ───────────────────────────────────

def test_persona_zero_of_THIS_application_is_allowed_with_no_row():
    """It is synthetic — there is no row to load. Requiring one would refuse every
    estate that runs on a stored form login."""
    v = pi.check_persona(persona_id=f"persona0::{ART}", persona=None, artifact_id=ART)
    assert v["allowed"] is True


def test_persona_zero_of_a_DIFFERENT_application_is_refused():
    """Its id encodes the artifact it belongs to; that suffix is the only thing
    stopping one app's stored login being replayed against another's."""
    v = pi.check_persona(persona_id="persona0::art-OTHER", persona=None, artifact_id=ART)
    assert v["allowed"] is False
    assert v["reason"] == "persona_belongs_to_another_application"


# ── every refusal is honest about who is at fault ────────────────────────────

def test_no_refusal_ever_implicates_the_application_under_test():
    cases = [
        ("p-1", _p(status="retired"), ART),
        ("p-1", _p(artifact_id="art-OTHER"), ART),
        ("p-ghost", None, ART),
        ("persona0::art-OTHER", None, ART),
    ]
    for pid, p, art in cases:
        v = pi.check_persona(persona_id=pid, persona=p, artifact_id=art)
        assert v["allowed"] is False
        note = v["detail"]["note"]
        assert "NOT an application failure" in note
        assert note.rstrip().endswith(".")


def test_every_refusal_names_the_member_it_refused():
    for pid, p in (("p-1", _p(status="retired")),
                   ("p-1", _p(artifact_id="art-OTHER")),
                   ("p-ghost", None)):
        v = pi.check_persona(persona_id=pid, persona=p, artifact_id=ART)
        assert v["detail"]["persona_id"] == pid


def test_a_refusal_carries_no_secret():
    """Persona rows carry no credential, but the detail is logged and returned —
    pin that nothing beyond identity travels."""
    p = _p(status="retired")
    p["description"] = "notes"
    v = pi.check_persona(persona_id="p-1", persona=p, artifact_id=ART)
    assert set(v["detail"]) <= {"persona_id", "artifact_id", "persona_artifact_id",
                                "persona_name", "status", "note"}


# ── wired at DISPATCH, and retirement actually stops things ──────────────────

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_STORE = open("app/services/test_factory/persona_store.py", encoding="utf-8").read()


def _seg(text, start, end=None):
    s = text[text.index(start):]
    return s[:s.index(end)] if end and end in s else s


def test_dispatch_checks_the_member_before_anything_else_happens():
    assert "from ..services.test_factory import persona_identity" in _ROUTER
    seg = _seg(_ROUTER, "# F5 — RUN AS A REAL, CURRENT MEMBER")
    assert "persona_identity.check_persona(" in seg[:1200]
    assert '"blocked_reason": "member_identity"' in seg[:1600]
    assert '"scripts": 0' in seg[:1600]


def test_the_member_check_runs_BEFORE_a_reservation_is_taken():
    """A refusal after acquire_reservation would leave a decommissioned member held
    by a reservation nothing releases."""
    body = _seg(_ROUTER, "# F5 — RUN AS A REAL, CURRENT MEMBER")
    assert body.index('"blocked_reason": "member_identity"') < body.index("acquire_reservation(")


def test_retiring_a_member_releases_its_hold_and_blocks_runs_in_flight():
    """Retirement used to only set a status. The member vanished from the picker
    while a run kept authenticating as it, and its reservation stayed held against
    the tenant's concurrency cap until the TTL expired."""
    seg = _seg(_ROUTER, "async def retire_persona_endpoint")
    assert "live_runs_for_persona(" in seg
    assert "release_persona_reservations(" in seg
    assert '"blocked"' in seg and "member_identity" in seg
    # the run's durable record is updated too, not just the in-memory one
    assert "_persist_job(" in seg
    # and the caller is told what happened rather than a bare {"retired": true}
    assert '"runs_blocked"' in seg and '"reservations_released"' in seg


def test_the_store_scopes_both_new_queries_to_the_tenant():
    for fn in ("live_runs_for_persona", "release_persona_reservations"):
        seg = _seg(_STORE, f"async def {fn}(")
        assert "TpPersonaReservationRow.tenant_id == tenant_id" in seg[:900], fn
        assert "released_at.is_(None)" in seg[:900], fn
