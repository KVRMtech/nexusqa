"""A recorded session cannot be scheduled — only a member can.

Phase 5 of Record + Run. Record + Run authenticates with a captured session, and a
session is perishable: it expires and nothing can replay it. The moment the operator
closes the tab, tomorrow's scheduled run has no way to be that user again. This is
the bridge from "I logged in once" to "this runs unattended".

What makes the bridge short rather than a hand-authoring exercise is that the
recording ALREADY derived the recipe — the choreography, which does not expire. The
recording deliberately captures identifiers only and never a credential value, so
the values cannot be harvested from it; the operator supplies them once, against the
fields the application's own login asked for.

Source-level checks: the endpoint composes existing, separately-tested primitives
(check_card, save_persona, save_persona_credential), so what is worth pinning is the
ORDER and the refusals — which is exactly what a later edit silently breaks.
"""
from app.services.test_factory import card_contract

_ROUTER = open("app/routers/test_factory.py", encoding="utf-8").read()
_NEXT = chr(10) + "async def "


def _handler(name: str) -> str:
    i = _ROUTER.index("async def %s(" % name)
    nxt = _ROUTER.find(_NEXT, i + 10)
    return _ROUTER[i:nxt if nxt > 0 else len(_ROUTER)]


H = _handler("member_from_recorded_login_endpoint")


# ── the card must be able to perform the login ───────────────────────────────

def test_a_member_is_never_created_without_a_recipe():
    """A card is values FOR a recipe. With no recipe there are no slots, so any
    names supplied are a guess — and a guessed name silently SKIPS the login."""
    try:
        card_contract.check_card(recipe=None, slot_values={"email": "a@b.c"})
        raise AssertionError("expected a refusal")
    except card_contract.CardContractError as exc:
        assert exc.detail["reason"] == "no_recipe"


def test_the_contract_is_checked_BEFORE_anything_is_created():
    """A rejected card must not leave a member behind that can never log in."""
    assert H.index("card_contract.check_card(") < H.index("persona_store.save_persona(")


def test_a_card_that_cannot_fill_the_login_is_refused_with_the_reason():
    assert "except card_contract.CardContractError as exc:" in H
    assert "status_code=422" in H
    assert "**exc.detail" in H


# ── atomicity ────────────────────────────────────────────────────────────────

def test_a_member_is_never_left_without_the_card_it_was_created_for():
    """save_persona and save_persona_credential share one transaction and ONE
    commit. If the card fails, the member must go with it."""
    assert H.count("await session.commit()") == 1
    assert H.index("persona_store.save_persona_credential(") < H.index("await session.commit()")
    card_err = H.index("except (ValueError, RuntimeError) as exc:")
    assert card_err < H.index("await session.commit()"), "the card failure must abort before commit"


# ── the retired-name trap ────────────────────────────────────────────────────

def test_reusing_a_retired_members_name_is_refused():
    """save_persona upserts by name and the upsert does not touch `status`, so this
    would attach the card to a member every dispatch then refuses — after telling
    the operator it saved fine."""
    assert "include_retired=True" in H
    assert '"name_retired"' in H
    assert "status_code=409" in H


def test_the_retired_refusal_does_not_silently_revive_the_member():
    """Retirement is a decision. Reviving it as a side effect of saving a card
    would defeat it."""
    assert 'status="active"' not in H
    assert "un-retire" in H


# ── never claims more than it proved ─────────────────────────────────────────

def test_the_new_member_is_NOT_marked_proven():
    """The recording proved a SESSION. It did not prove that these values, typed
    into this recipe, log in. Only a replay proves that."""
    assert '"verify_status": "unverified"' in H
    assert '"proven": False' in H


def test_the_response_says_how_to_actually_prove_it():
    assert "Verify" in H
    assert "proved a session, not" in H


def test_the_stored_card_is_unverified_by_construction():
    """Not merely reported as unverified — written that way, so no path can stamp
    it proven without a replay."""
    store = open("app/services/test_factory/persona_store.py", encoding="utf-8").read()
    seg = store[store.index("async def save_persona_credential"):]
    seg = seg[:seg.index("async def get_persona_credential")]
    assert 'verify_status="unverified"' in seg
    assert '"verify_status": "unverified"' in seg, "and on the conflict path too"


# ── secrets ──────────────────────────────────────────────────────────────────

def test_no_credential_VALUE_is_ever_returned():
    """The values travel one way. Only names come back."""
    tail = H[H.rindex("return {"):]
    assert '"slot_names"' in tail
    assert "slot_values" not in tail


def test_the_audit_records_the_member_not_the_secret():
    assert "member_saved_from_recorded_login" in H
    seg = H[H.index("_persona_audit("):]
    assert "slot_values" not in seg[:400]


def test_it_refuses_rather_than_store_a_credential_in_plaintext():
    assert "if envelope is None:" in H
    assert "503" in H


# ── access ───────────────────────────────────────────────────────────────────

def test_saving_a_member_requires_a_write_role():
    assert "_persona_write_ok(user)" in H
    assert "403" in H


def test_a_blank_name_is_refused():
    assert 'if not name:' in H


def test_the_member_is_scoped_to_the_artifact():
    assert "_require_artifact(session, artifact_id, tenant_id)" in H
