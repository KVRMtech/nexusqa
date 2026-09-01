"""Credential-card contract — a card must be able to fill the login it is for.

A login recipe declares the slots its steps fill. A credential card supplies the
values. Nothing checked that the two agreed, and the operator was asked to RETYPE
the slot names into a free-text box pre-filled with a vocabulary the customer's app
may not even use.

A mismatch is not caught anywhere downstream. The card saves cleanly, the manifest
shows it, and at run time the compiled login finds the slot missing and SKIPS THE
WHOLE LOGIN — so the suite runs unauthenticated and every failure is attributed to
the application under test. A typo becomes "your app is broken".

So the contract is enforced here, at the API, not in the form. The form is a
convenience; a card is also reachable from a script, a bulk import, or curl, and
every one of those paths must be unable to store a card that cannot log in.

WHAT IS AND IS NOT AN ERROR
  * a MISSING required slot            -> refused; the login cannot be performed
  * an UNEXPECTED slot                 -> refused; it means the operator is filling a
                                          different login than the one on file, which
                                          is exactly the state that silently skips
  * a slot present but EMPTY           -> refused; an empty secret is not a credential,
                                          and "" would be typed into the field
  * NO recipe on the artifact yet      -> refused; a card provisioned before a login is
                                          recorded is guaranteed-wrong work, and the
                                          slot names would be a guess

Pure — no DB, no I/O. The caller loads the recipe and applies the verdict.
"""
from __future__ import annotations

__all__ = ["required_slots", "optional_slots", "slot_fields", "check_card",
           "check_recipe_steps", "CardContractError"]


class CardContractError(ValueError):
    """A card that cannot fill its recipe. Carries the detail an operator needs to
    fix it — which names are missing, which are unexpected, and what the login
    actually asks for."""

    def __init__(self, message: str, detail: dict):
        super().__init__(message)
        self.detail = detail


def _fill_slots(recipe: dict | None, *, optional: bool) -> list:
    """Slot names filled by this recipe's steps, in first-seen order, selecting
    either the unconditional steps or the optional ones."""
    out: list = []
    seen: set = set()
    for step in ((recipe or {}).get("steps") or []):
        if not isinstance(step, dict) or step.get("action") != "fill":
            continue
        if bool(step.get("optional")) is not optional:
            continue
        name = str(step.get("slot") or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def required_slots(recipe: dict | None) -> list:
    """The slot names a card MUST supply, in first-seen order.

    Derived from the STEPS, not the declared ``slots`` list, because the steps are
    what the replay executes: a recipe whose steps fill a slot absent from ``slots``
    (hand-edited, or partially migrated) would otherwise pass a check against the
    declaration and still skip the login. Falls back to the declaration when the
    steps carry no fills.

    OPTIONAL steps are excluded. They exist for screens only SOME members are shown
    — the verify-documents interstitial being the recorded example — and demanding a
    value for a screen a member never reaches would refuse exactly the members the
    optional marking was introduced to accommodate."""
    rec = recipe or {}
    out = _fill_slots(rec, optional=False)
    if out:
        return out
    if _fill_slots(rec, optional=True):
        # Every fill is conditional; nothing is unconditionally required.
        return []
    seen: set = set()
    for slot in (rec.get("slots") or []):
        name = str((slot or {}).get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def optional_slots(recipe: dict | None) -> list:
    """Slots filled only by OPTIONAL steps — supply them if you have them, but a
    card without them is not broken."""
    req = set(required_slots(recipe))
    return [s for s in _fill_slots(recipe, optional=True) if s not in req]


def slot_fields(recipe: dict | None) -> list:
    """The card form, described by the recipe: ``[{name, label, type}]``.

    The LABEL is the one the app itself showed at record time — carried on the fill
    step — so the operator sees their own application's wording ("Member number",
    "Policy #") rather than our internal slot identifier. The slot name is still
    what the card is keyed by; the label is only how it reads.

    Every observed slot is stored ``secret`` by the recorder (a plain slot would be
    exempted by the interpreter's missing-credential guard and could green-wash), so
    the type here is advisory for masking, and defaults to masked when unknown."""
    rec = recipe or {}
    labels: dict = {}
    for step in (rec.get("steps") or []):
        if isinstance(step, dict) and step.get("action") == "fill":
            name = str(step.get("slot") or "").strip()
            if name and name not in labels:
                labels[name] = str(step.get("label") or "").strip()
    types = {str((s or {}).get("name") or "").strip(): str((s or {}).get("type") or "").strip()
             for s in (rec.get("slots") or [])}
    return [{"name": name, "label": labels.get(name) or name,
             "type": types.get(name) or "secret"}
            for name in required_slots(rec)]


def check_recipe_steps(steps: list | None) -> None:
    """Refuse a recipe whose ``assert_home`` step asserts nothing.

    ``assert_home`` is the ONE thing that can mark a login proven: the interpreter
    sets ``homeAsserted`` in that branch and prints ``home reached``, and that token
    is what stamps a card ``verified``. But the branch waits only on the signals the
    step carries — ``url_pattern`` / ``selector`` / ``expect_text`` — so a step with
    NONE of them awaits nothing, cannot throw, and reports proof for a login that
    was never checked.

    The recorder already refuses to emit a signal-less oracle. This closes the other
    door: ``POST …/recipes`` accepts hand-authored steps and validated none of them,
    so a signal-less ``assert_home`` was a one-line way to forge proof for the whole
    fleet.
    """
    for i, step in enumerate(steps or [], start=1):
        if not isinstance(step, dict) or step.get("action") != "assert_home":
            continue
        if not any(str(step.get(k) or "").strip()
                   for k in ("url_pattern", "selector", "expect_text")):
            raise CardContractError(
                f"step {i}: assert_home has nothing to assert",
                {"reason": "signal_less_home_oracle", "step": i,
                 "note": ("The logged-in checkpoint must name something observable — "
                          "a url_pattern, a selector, or expected text. A checkpoint "
                          "with none of these asserts nothing and would report every "
                          "login as proven, including logins that never happened. "
                          "Re-record the login from the logged-in page, or give this "
                          "step a signal.")},
            )


def check_card(*, recipe: dict | None, slot_values: dict | None) -> dict:
    """Raise ``CardContractError`` unless this card can fill this recipe.

    Returns the accepted ``{slot_names, required}`` when it can."""
    # ``None`` is normalised to '' rather than stringified — ``str(None)`` is the
    # four-character word "None", which would read as a perfectly good secret here
    # and then get TYPED INTO THE FIELD at replay.
    supplied = {str(k).strip(): ("" if v is None else str(v))
                for k, v in (slot_values or {}).items() if str(k).strip()}

    if not recipe:
        raise CardContractError(
            "no login recipe on this artifact — record the login first",
            {"reason": "no_recipe",
             "note": ("A credential card supplies values for the slots a recorded "
                      "login asks for. With no recipe there are no slots to fill, so "
                      "any names entered now would be a guess — and a guessed name "
                      "silently skips the login at run time. Record the login first.")},
        )

    required = required_slots(recipe)
    if not required:
        raise CardContractError(
            "the login recipe fills no fields — nothing for a card to supply",
            {"reason": "recipe_has_no_slots",
             "note": ("This recipe performs no fill steps, so it cannot be driven by a "
                      "credential card. Re-record the login.")},
        )

    missing = [name for name in required if not str(supplied.get(name, "")).strip()]
    # A slot filled only by an OPTIONAL step is legitimately present on a card, so
    # it is not "unexpected" even though it is not required.
    unexpected = sorted(set(supplied) - set(required) - set(optional_slots(recipe)))

    if missing or unexpected:
        # Both are refused for the same reason: either one means the card and the
        # login disagree, and a disagreeing card is precisely what runs the suite
        # logged out while blaming the application.
        raise CardContractError(
            "this card does not match the recorded login",
            {
                "reason": "slots_do_not_match_recipe",
                "required": required,
                "missing": missing,
                "unexpected": unexpected,
                "note": ("The recorded login fills " + ", ".join(required) +
                         ". A card that does not supply exactly those is not stored: "
                         "at run time the missing slot would skip the entire login, the "
                         "suite would execute logged out, and the failures would be "
                         "attributed to the application. Slot names come from the app's "
                         "own form — take them from the recipe, do not type them."),
            },
        )

    return {"slot_names": required, "required": required}
