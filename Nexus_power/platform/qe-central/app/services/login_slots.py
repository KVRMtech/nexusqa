"""Turn the values a human supplies for an OBSERVED login into replayable credentials.

Recording a login captures the choreography and the slot NAMES the app asked for —
never the values typed (``infrastructure/runner/login_observer.js`` is value-free by
construction, and says so in the UI). That is a deliberate security property, but on its
own it leaves a recording able to replay only a SESSION: a snapshot that expires, and
that an app whose login lives in client-side state can never restore. Such an app was
recorded three times and never once crawled authenticated.

So the recording asks for the values it just watched the app require, and this module
maps ``{slot name -> value}`` onto the shape the crawler replays forever
(``engines/qe-explorer/app/auth.py::Credentials.from_payload``)::

    {"username": ..., "password": ..., "mfa": {"kind": "otp", "otp": ...}}

Generic by construction — the slots come from what the app ACTUALLY asked for, so an
email+password login, a member#+PIN login (U6) and a 3-slot MFA login all map without
any app-specific knowledge. Pure: no I/O, no DB, no logging (values are secret).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

#: A SECOND FACTOR — a code that changes per sign-in. Deliberately does NOT include the
#: bare words "pin"/"passcode"/"code": on a passwordless member#+PIN login the PIN is the
#: PRIMARY secret, not a second factor, and mis-classifying it would leave the login with
#: no password at all.
_OTP_HINTS: tuple[str, ...] = (
    "otp", "one-time", "one time", "onetime", "mfa", "2fa", "two-factor", "two factor",
    "verification", "verify code", "authenticator", "auth code", "security code",
    "sms code", "email code", "confirmation code", "one time code",
)
#: The PRIMARY secret when the field is not a masked input (a PIN/passcode text box).
_SECRET_NAME_HINTS: tuple[str, ...] = ("password", "passcode", "pin", "secret")


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _matches(name: Any, hints: Sequence[str]) -> bool:
    n = _norm(name)
    return bool(n) and any(h in n for h in hints)


def _slot_pairs(slots: Any) -> list[tuple[str, str]]:
    """``[(name, type)]`` from a recipe's ``slots`` (or a bare ``slot_names`` list)."""
    out: list[tuple[str, str]] = []
    for s in (slots or ()):
        if isinstance(s, Mapping):
            name = str(s.get("name") or "").strip()
            kind = _norm(s.get("type"))
        else:
            name, kind = str(s or "").strip(), ""
        if name:
            out.append((name, kind))
    return out


def credentials_from_slot_values(
    slots: Any, slot_values: Mapping[str, Any] | None,
) -> dict:
    """Map ``{slot name -> value}`` onto ``{username, password, mfa}``.

    Classification, in order (each slot is claimed once):

    * **mfa**      — a slot whose name is a SECOND-FACTOR hint (``mfaCode``,
      ``Verification code``, ``OTP``). Stored as a deterministic ``{"kind": "otp"}``
      code, which is what a test environment issues.
    * **password** — the first MASKED slot (``type == "secret"``); when a login has no
      masked field, the first slot NAMED like a secret (a member#+PIN login).
    * **username** — the first slot left over: the identifier the app asked for.

    Blank values are ignored, so a partially-filled form contributes only what it has.
    Returns ``{}`` when nothing usable was supplied.
    """
    pairs = _slot_pairs(slots)
    values = {str(k): v for k, v in (slot_values or {}).items()}
    if not pairs:
        # No recipe to lean on: fall back to the value keys themselves, so a caller that
        # only knows the names it prompted with still gets a correct mapping.
        pairs = [(k, "") for k in values]

    def _value(name: str) -> str:
        return str(values.get(name) or "")

    otp_slot = next((n for n, _t in pairs if _matches(n, _OTP_HINTS)), "")
    password_slot = next(
        (n for n, t in pairs if t == "secret" and n != otp_slot), "",
    )
    if not password_slot:
        password_slot = next(
            (n for n, _t in pairs if n != otp_slot and _matches(n, _SECRET_NAME_HINTS)),
            "",
        )
    username_slot = next(
        (n for n, _t in pairs if n not in (otp_slot, password_slot)), "",
    )

    creds: dict = {}
    if username_slot and _value(username_slot):
        creds["username"] = _value(username_slot)
    if password_slot and _value(password_slot):
        creds["password"] = _value(password_slot)
    if otp_slot and _value(otp_slot):
        # A fixed/deterministic code — what a QA environment issues. A TOTP seed is a
        # different thing and stays an explicit choice in the onboarding wizard.
        creds["mfa"] = {"kind": "otp", "otp": _value(otp_slot)}
    return creds
