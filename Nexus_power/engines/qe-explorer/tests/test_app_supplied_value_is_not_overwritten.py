"""AN APPLICATION'S OWN VALID DATA OUTRANKS A GUESS WE CANNOT JUSTIFY.

WHAT WAS WRONG.  ``resolve_field`` ends its semantic ladder with a STRUCTURAL
fallback -- ``_synthesize_default``, reached only when the semantic vocabulary
produced nothing, i.e. exactly when we did NOT recognise the field.  It fired
even when the application had already put a perfectly good value in the box,
so an unrecognised field was overwritten by the weakest guess in the system.

MEASURED (LifeOps, 2026-08-27, from 7d7408b).  The application's own
application wizard ships valid data in every field.  Left untouched it advances
cleanly.  The crawl overwrote all 36 of 36 fields -- ``field_ledger`` recorded
``provenance: synthesized`` for every one, none preserved -- and stalled on
step 4 of the wizard:

    field   : "Weight"  (application shipped "178")
    verdict : semantic_type=free_text, basis=structural, confidence=0.4
    app said: "Enter a valid weight."   (code=pattern, aria-errormessage)

A form that arrives already valid is the EASIEST case there is, and the engine
made it harder than a blank one.  The value we destroyed was better evidence
than the value we invented, and we had no confidence in the replacement.

THE RULE.  When the semantic ladder yields nothing AND the application has
already committed a non-empty value, keep the application's value and declare
it as such (``PROV_APP_SUPPLIED``).  It is re-typed rather than skipped, so
every downstream read-back, intent verification and ledger contract is
unchanged -- only the provenance, and the value's survival, differ.

Scope is deliberately narrow: this rung sits BELOW every semantic rung, so a
field we DO understand is still exercised with the crawl's fictional identity.
Recognising a field is the licence to replace its value; failing to recognise
it is not.

WHAT IS ASSERTED HERE.  The defect and both controls, because a rule that
preserves everything would stop the crawl filling forms at all.
"""
from __future__ import annotations

from app.field_values import DATA_MODE_AGENT
from app.forms import (PROV_APP_SUPPLIED, PROV_SYNTHESIZED, AnswerKey,
                       resolve_field)
from app.identity_pack import derive as derive_identity

_ID = derive_identity("test-seed")


def _resolve(control, kind="text", **kw):
    return resolve_field(control, kind, control["name"], AnswerKey({}), _ID, **kw)


def test_an_unrecognised_field_keeps_the_value_the_application_shipped():
    """THE DEFECT, with the measured field.  "Weight" is not in the semantic
    vocabulary; the application's own "178" must survive."""
    control = {"name": "Weight", "kind": "text", "input_type": "text",
               "value_committed": "178"}
    out = _resolve(control, data_mode=DATA_MODE_AGENT)
    assert out["value"] == "178", (
        f"the application's valid value was replaced by {out['value']!r}")
    assert out["entry"]["provenance"] == PROV_APP_SUPPLIED
    assert out["entry"]["filled"] is True


def test_an_unrecognised_field_that_is_EMPTY_is_still_synthesized():
    """CONTROL ONE.  The rung must key on the application having supplied
    something -- not on the field being unrecognised.  If this goes red the
    crawl has stopped filling blank forms, which is its whole job."""
    control = {"name": "Weight", "kind": "text", "input_type": "text",
               "value_committed": ""}
    out = _resolve(control, data_mode=DATA_MODE_AGENT)
    assert out["value"] not in (None, ""), "a blank unrecognised field went unfilled"
    assert out["entry"]["provenance"] == PROV_SYNTHESIZED


def test_a_recognised_field_is_still_exercised_even_when_prefilled():
    """CONTROL TWO.  Recognising a field is the licence to replace its value.
    An email we understand is still filled from the crawl's fictional identity,
    so coverage of known semantics is unchanged."""
    control = {"name": "Email", "kind": "text", "input_type": "email",
               "value_committed": "morgan.lee@example.com"}
    out = _resolve(control, data_mode=DATA_MODE_AGENT)
    assert out["entry"]["provenance"] == PROV_SYNTHESIZED
    assert out["value"] != "morgan.lee@example.com"
