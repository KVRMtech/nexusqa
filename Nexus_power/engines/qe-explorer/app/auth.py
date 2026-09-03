"""QE-Central Contained Explorer — AUTHENTICATION (design §3.2 auth.py).

Inventory-matched login that logs in like a user and PROVES it worked, under
the fail-closed guard's narrow AUTH window:

  * controls are matched by ACCESSIBLE NAME (:func:`match_login_controls`) — the
    username field, the password field (``input_type=='password'``), and a
    submit control whose name is a sign-in verb and is NOT an irreversible
    refuse-pack verb;
  * the login submit is permitted by the guard only within the AUTH window
    (:class:`AuthWindow`): same registrable domain, ≤ ``auth_max_requests``
    requests within ``auth_window_ms`` of the submit (design §3.2 / config);
  * success is VERIFIED, never assumed (:func:`verify_login_success`): the state
    fingerprint changed AND no password field remains AND no error live-region
    is showing — anything less is an honest failure;
  * the captured ``storage_state`` is held IN MEMORY only (never written to
    disk) and relayed to qe-central for the encrypted E3 handoff;
  * re-login on session expiry is bounded to ``max_relogins`` (config, default 3).

Credentials are NEVER logged, never written to the manifest raw (the username
is PII-scrubbed like any value; the password value is emptied at source).
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import logging
import re
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import emit
from .browser import BrowserPort, PageObservation, RawObservation
from .state_identity import StateFingerprinter
from .guard import Phase, classify_action_verb
from .inventory import build_inventory

logger = logging.getLogger(__name__)

#: Default accessible-name hints for the USERNAME field (config/data-driven —
#: extend via the explore request, never hard-code a client's labels).
DEFAULT_USERNAME_HINTS: tuple[str, ...] = (
    "username", "user name", "user id", "userid", "email", "e-mail",
    "login", "account", "member id", "user",
)
#: BROADER identifier hints for RECOGNISING a username-first STEP-1 across member /
#: policy / customer portals (the beachhead) WITHOUT broadening the login username
#: matcher itself (match_login_controls keeps DEFAULT_USERNAME_HINTS). Value-free stems.
IDENTIFIER_STEP_HINTS: tuple[str, ...] = DEFAULT_USERNAME_HINTS + (
    "member", "membership", "member number", "member no", "policy", "policy number",
    "policy no", "customer", "customer id", "customer number", "subscriber",
    "account number", "national id", "ssn", "social security", "phone", "mobile",
)
#: Default accessible-name hints for the SUBMIT control.  Includes "continue" /
#: "next" so a MULTI-STEP login (username page → password page) and an MFA step
#: (enter code → verify) are advanced by the same matcher.
DEFAULT_SUBMIT_HINTS: tuple[str, ...] = (
    "sign in", "signin", "log in", "login", "log on", "logon",
    "continue", "submit", "next", "go", "enter", "verify", "confirm",
)
#: REGISTRATION-verb hints. A page carrying one of these (or two password fields) is a
#: public SIGN-UP page, NOT a login wall — a credential-less crawl must explore it, never
#: stop. Kept distinct from the login submit verbs above.
DEFAULT_SIGNUP_HINTS: tuple[str, ...] = (
    "create account", "create an account", "create your account", "create profile",
    "sign up", "signup", "register", "registration", "join now", "enroll",
    "get started", "new account", "open account", "open an account",
)
#: Accessible-name hints for a ONE-TIME-CODE / OTP field (MFA second factor).  A
#: field matching these that is NOT a password is filled with the computed code.
DEFAULT_OTP_HINTS: tuple[str, ...] = (
    "one-time", "one time", "onetime", "otp", "passcode", "pass code",
    "verification code", "verification", "verify code", "security code",
    "authentication code", "authenticator", "6-digit", "6 digit", "digit code",
    "2fa", "two-factor", "two factor", "mfa code", "sms code", "email code",
    "confirmation code", "access code", "token", "pin",
)
#: Accessible-name hints for choosing an OTP DELIVERY method (email / mobile /
#: text).  Acted on ONLY when the auth profile explicitly names a delivery, so
#: the crawler never guesses which channel to trigger.
DEFAULT_DELIVERY_HINTS: tuple[str, ...] = (
    "email", "e-mail", "mobile", "phone", "text", "sms", "call", "authenticator app",
)

_PASSWORD_INPUT_TYPES = frozenset({"password"})
#: Inventory ``kind`` values a fillable text-like CREDENTIAL field may have (OTP
#: fields are often ``text`` / ``tel`` / ``number``).  A native ``<select>`` is
#: NEVER a username/OTP field: including it here made ``_match_username_control``
#: fall back to a page's first dropdown (e.g. a "Product" quote select) and call
#: ``Locator.fill()`` on it — which errors ("Element is not an <input>…"), so the
#: crawl recorded a valueless ``type`` action and aborted ``auth_failed``. Selects
#: are exercised correctly by the FORMS path (``_fill_one`` → ``select_option``).
_TEXT_LIKE_KINDS = frozenset({"text", "date", "number", "tel", "search", "email"})


def _pad_b32(seed: str) -> str:
    """Upper-case, strip spaces, and pad a base32 TOTP secret to a multiple of 8."""
    s = re.sub(r"\s+", "", seed).upper()
    return s + "=" * ((8 - len(s) % 8) % 8)


def totp_code(
    seed_b32: str, *, digits: int = 6, period: int = 30, at_unix: Optional[float] = None,
) -> str:
    """Compute the current RFC-6238 TOTP code for a base32 secret (stdlib only).

    Deterministic for a given ``at_unix`` (tests pin it); uses wall-clock time in
    production.  Returns ``""`` on an unparseable seed rather than raising — a bad
    seed becomes an honest login failure downstream, never a crash.  No third-party
    dependency: the explorer image stays lean and auditable.
    """
    try:
        key = base64.b32decode(_pad_b32(seed_b32), casefold=True)
    except (binascii.Error, ValueError):
        return ""
    if not key:
        return ""
    counter = int((time.time() if at_unix is None else at_unix) // max(1, int(period)))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    d = max(4, min(10, int(digits)))
    return str(binary % (10 ** d)).zfill(d)


@dataclass(frozen=True)
class MfaConfig:
    """The second factor the login must satisfy (all data-driven, never hard-coded).

    ``kind='totp'`` computes the code from the shared ``seed`` (authenticator-app
    MFA — fully automatable without a phone).  ``kind='otp'`` uses a FIXED ``otp``
    (a deterministic test code, e.g. USAA's ``123456`` in a test env).  ``delivery``
    (optional) names the channel to pick on a "how do you want your code?" screen —
    acted on only when set, so the crawler never triggers the wrong channel.
    """

    kind: str  # "totp" | "otp"
    seed: str = ""
    otp: str = ""
    delivery: str = ""
    digits: int = 6
    period: int = 30

    def current_code(self, *, at_unix: Optional[float] = None) -> str:
        """The code to type NOW: a live TOTP from the seed, or the fixed OTP."""
        if self.kind == "totp" and self.seed:
            return totp_code(self.seed, digits=self.digits, period=self.period, at_unix=at_unix)
        return (self.otp or "").strip()

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]]) -> "Optional[MfaConfig]":
        if not isinstance(payload, Mapping):
            return None
        kind = str(payload.get("kind") or payload.get("type") or "").strip().lower()
        seed = str(payload.get("seed") or payload.get("totp_seed") or "").strip()
        otp = str(payload.get("otp") or payload.get("code") or "").strip()
        if kind not in ("totp", "otp"):
            kind = "totp" if seed else ("otp" if otp else "")
        if not kind or (kind == "totp" and not seed) or (kind == "otp" and not otp):
            return None
        try:
            digits = int(payload.get("digits") or 6)
            period = int(payload.get("period") or 30)
        except (TypeError, ValueError):
            digits, period = 6, 30
        return cls(
            kind=kind, seed=seed, otp=otp,
            delivery=str(payload.get("delivery") or "").strip().lower(),
            digits=digits, period=period,
        )


@dataclass(frozen=True)
class Credentials:
    """Login secret + optional MFA + accessible-name hints (all data-driven).

    Backward compatible: a bare ``{username, password}`` payload logs in exactly
    as before (single-step, no MFA).  An ``mfa`` block adds a second factor and a
    multi-step sequence is handled automatically by the authenticator's step loop.
    """

    username: str
    password: str
    username_hints: tuple[str, ...] = DEFAULT_USERNAME_HINTS
    submit_hints: tuple[str, ...] = DEFAULT_SUBMIT_HINTS
    otp_hints: tuple[str, ...] = DEFAULT_OTP_HINTS
    delivery_hints: tuple[str, ...] = DEFAULT_DELIVERY_HINTS
    mfa: Optional[MfaConfig] = None

    #: U6 — a login is not always username+password. Accept the primary
    #: IDENTIFIER under any of these keys (member#+PIN, email-first, policy_no…),
    #: and the SECRET under any of these. Value-free: keys, never values.
    _IDENTIFIER_KEYS = ("username", "user", "userid", "user_id", "login", "email",
                        "member_number", "member", "member_id", "policy_no",
                        "policy_number", "subscriber_id")
    _SECRET_KEYS = ("password", "pass", "pin", "passcode", "secret", "security_code")

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]]) -> "Optional[Credentials]":
        """Build from the explore-request ``credentials`` object, or ``None``.

        U6 — passwordless / alternate-identifier logins: falls back to identifier
        and secret aliases, and allows an EMPTY secret when an MFA block or an
        explicit ``passwordless`` flag is present (OTP-first / magic-link). Refuses
        only when there is no identifier at all. A bare ``{username, password}``
        still logs in exactly as before.
        """
        if not payload:
            return None
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not username:
            for k in cls._IDENTIFIER_KEYS:
                v = str(payload.get(k) or "").strip()
                if v:
                    username = v
                    break
        if not password:
            for k in cls._SECRET_KEYS:
                v = str(payload.get(k) or "")
                if v:
                    password = v
                    break
        mfa = MfaConfig.from_payload(payload.get("mfa"))
        passwordless = bool(payload.get("passwordless"))
        if not username:
            return None                       # no identifier → cannot log in
        if not password and not (mfa or passwordless):
            return None                       # a secret is required unless MFA/passwordless
        uh = tuple(str(h).strip().lower() for h in (payload.get("username_hints") or ()) if str(h).strip())
        sh = tuple(str(h).strip().lower() for h in (payload.get("submit_hints") or ()) if str(h).strip())
        oh = tuple(str(h).strip().lower() for h in (payload.get("otp_hints") or ()) if str(h).strip())
        dh = tuple(str(h).strip().lower() for h in (payload.get("delivery_hints") or ()) if str(h).strip())
        return cls(
            username=username,
            password=password,
            username_hints=uh or DEFAULT_USERNAME_HINTS,
            submit_hints=sh or DEFAULT_SUBMIT_HINTS,
            otp_hints=oh or DEFAULT_OTP_HINTS,
            delivery_hints=dh or DEFAULT_DELIVERY_HINTS,
            mfa=mfa,
        )


@dataclass(frozen=True)
class LoginControls:
    """The three matched login controls (each a :class:`ControlRecord` dict)."""

    username: dict[str, Any]
    password: dict[str, Any]
    submit: dict[str, Any]


@dataclass
class AuthResult:
    """Outcome of one login attempt — grounded, honest, credential-free."""

    success: bool
    reason: str
    actions: list[emit.ActionRecord] = field(default_factory=list)
    storage_state: Optional[dict[str, Any]] = None
    before_fingerprint: str = ""
    after_fingerprint: str = ""
    #: True iff a login secret (password or OTP) was actually submitted to a form
    #: — i.e. a REAL authentication gate was present and driven. False when no
    #: login form was found/completed (an accessible public page the operator
    #: pointed us at, with credentials configured for OTHER, gated areas). The
    #: crawler uses this to tell a genuine login FAILURE (abort, honest) apart
    #: from a public page with no login (explore unauthenticated + loud warning).
    secret_submitted: bool = False


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _is_password(control: Mapping[str, Any]) -> bool:
    it = _norm(control.get("input_type")) or _norm((control.get("qec") or {}).get("input_type"))
    return it in _PASSWORD_INPUT_TYPES


def _name_matches_any(name: str, hints: Sequence[str]) -> bool:
    n = _norm(name)
    if not n:
        return False
    return any(h and h in n for h in hints)


def _match_password_control(controls: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """The password field (``input_type=='password'``), preferring a named one."""
    named = next((c for c in controls if _is_password(c) and _norm(c.get("name"))), None)
    if named is not None:
        return named
    # A nameless password field still anchors the form (for the username
    # heuristic) but cannot itself be a fill target.
    return next((c for c in controls if _is_password(c)), None)


#: Secret-hint field names for a PIN / passcode that is NOT input_type=password —
#: the secret on a passwordless member#+PIN login (U6). Value-free (names only).
_SECRET_HINTS = ("pin", "passcode", "security code", "secret",
                 "personal identification", "access code")


def _match_secret_control(controls: Sequence[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """A PIN / passcode field to serve as the secret when a screen has NO password
    input (U6 — passwordless member#+PIN). A text-like field whose accessible name
    is a secret hint. Never a real password input (that path is handled above)."""
    for c in controls:
        if _is_password(c):
            continue
        if (_norm(c.get("kind")) in _TEXT_LIKE_KINDS
                and _name_matches_any(str(c.get("name")), _SECRET_HINTS)):
            return dict(c)
    return None


def _auth_identifiable(c: Optional[Mapping[str, Any]]) -> bool:
    """Can this login field be named in the evidence at all?

    The fill branches below required a non-empty ACCESSIBLE name, so that a
    nameless control is never typed into and then recorded as a question
    nobody can read. Right rule, wrong scope for a login form: measured on
    parabank.parasoft.com 2026-09-02, both login inputs have NO accessible
    name, the branches were skipped, and the crawl reported "no username field
    could be filled" while the fields sat there with name="username" and
    name="password" on them.

    A form control's own name= attribute IS a name for the evidence — it is
    what the application calls the field, it is stable, and it is what the port
    now binds the locator on. So a login field is identifiable by either.
    """
    if c is None:
        return False
    return bool(_norm(c.get("name")) or _control_name_attr(c))


def _control_name_attr(c: Mapping[str, Any]) -> str:
    """The field's own name= attribute, wherever the record carries it."""
    qec = c.get("qec")
    if isinstance(qec, Mapping):
        got = _norm(qec.get("name_attr"))
        if got:
            return got
    return _norm(c.get("name_attr"))


def _text_fields(controls: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Text-like fields this login sequence may drive.

    A field with NO accessible name is skipped everywhere else in the crawler,
    and rightly: a nameless control cannot be catalogued, so filling it would
    record a question nobody can read (``qec.forms.skip_nameless_field``).

    THE LOGIN FORM IS THE ONE PLACE THAT RULE IS FATAL. Measured on
    parabank.parasoft.com 2026-09-02:

        <p><b>Username</b></p>
        <div class="login"><input type="text" class="input" name="username"></div>

    no id, no aria-label, no <label for> — the input has no accessible name at
    all. The password field beside it IS found, because type="password" is a
    STRUCTURAL signal; the username field has no such signal, so it was dropped
    here and _match_username_control returned None. The crawl reported "no
    username field could be filled" and never authenticated: 8 public pages,
    the whole banking application unseen.

    So a nameless field is admitted HERE — and only here — when it declares a
    name= attribute, which is the application's own identifier for it and is
    what the port now binds on. The catalogue is unaffected: this list feeds
    the auth sequence, not the question inventory.
    """
    return [
        c for c in controls
        if _norm(c.get("kind")) in _TEXT_LIKE_KINDS and not _is_password(c)
        and (_norm(c.get("name")) or _control_name_attr(c))
    ]


def _hinted_username_control(
    controls: Sequence[Mapping[str, Any]],
    username_hints: Sequence[str] = DEFAULT_USERNAME_HINTS,
) -> Optional[Mapping[str, Any]]:
    """A username field matched BY HINT — no positional fallback.

    :func:`_match_username_control` ends in ``return text_fields[0]``, which is
    correct where it is used (the step loop, driving a screen already known to
    be a login) but wrong as evidence that a login form is PRESENT: it never
    answers None on any page carrying a text input.

    Measured (LifeOps, 2026-08-27): a landing page whose quote form has an
    ``Age`` field — normalised by ``build_inventory`` from role=spinbutton to
    kind="text" — claimed that field as the username, so the sign-in hop below
    was suppressed and three visible affordances ("Login", "Sign in", "Member
    login") were never clicked. A correct member number, PIN and OTP went
    unused and the authenticated application was never reached.

    Asking "is there a login form here?" needs a CONFIDENT match; a field that
    matched no hint is not one.
    """
    return next((c for c in _text_fields(controls)
                 if _name_matches_any(str(c.get("name")), username_hints)), None)


def _match_username_control(
    controls: Sequence[Mapping[str, Any]],
    username_hints: Sequence[str] = DEFAULT_USERNAME_HINTS,
    *,
    password: Optional[Mapping[str, Any]] = None,
) -> Optional[Mapping[str, Any]]:
    """Best hint-matched text field, falling back to the field just before the
    password (the near-universal layout).  A username-first (multi-step) screen
    has a username field and NO password — still matched here."""
    text_fields = _text_fields(controls)
    if not text_fields:
        return None
    # Hints match the accessible name first, then the name= attribute: for the
    # nameless case above, "username" is exactly what the application calls the
    # field, so the hint still identifies it rather than falling through to a
    # positional guess.
    hit = next((c for c in text_fields
                if _name_matches_any(str(c.get("name")), username_hints)
                or _name_matches_any(_control_name_attr(c), username_hints)), None)
    if hit is not None:
        return hit
    if password is not None:
        try:
            p_idx = list(controls).index(password)
        except ValueError:
            p_idx = len(controls)
        preceding = [c for c in text_fields if _index_of(controls, c) < p_idx]
        return preceding[-1] if preceding else text_fields[0]
    return text_fields[0]


def _match_submit_control(
    controls: Sequence[Mapping[str, Any]], submit_hints: Sequence[str] = DEFAULT_SUBMIT_HINTS,
    *, after: Optional[Mapping[str, Any]] = None,
) -> Optional[Mapping[str, Any]]:
    """The login form's own submit — a non-danger button named with a sign-in
    or advance verb.

    ``after`` anchors the search to a control belonging to the FORM (the
    password or username field), and the form's submit is looked for BELOW it
    before the page is searched as a whole.

    WHY THE ANCHOR EXISTS.  The hints are matched against an accessible name
    over every control on the page, and site chrome wins on DOM order.
    Measured (LifeOps, 2026-08-27): the sign-in screen carries a persistent top
    navigation whose items include a button named "Login", emitted long before
    the form's own "Continue":

        username -> Member ID     password -> PIN     SUBMIT -> Login

    Clicking that nav item re-rendered the same screen, so the sequence
    reported "the screen did not move, and was given time to" and the login
    failed with correct credentials already typed in.

    The fallback to a page-wide search is deliberate: a form whose submit sits
    ABOVE its fields is unusual but legal, and this must not stop finding it.
    """
    def _named_buttons(seq: Sequence[Mapping[str, Any]]):
        for c in seq:
            if _norm(c.get("kind")) != "button" or not _norm(c.get("name")):
                continue
            if c.get("danger"):  # never choose an irreversible-verb button as submit
                continue
            if _name_matches_any(str(c.get("name")), submit_hints):
                yield c

    if after is not None:
        anchor = _index_of(controls, after)
        below = next(_named_buttons(list(controls)[anchor + 1:]), None)
        if below is not None:
            return below
    return next(_named_buttons(controls), None)


def match_login_controls(
    controls: Sequence[Mapping[str, Any]],
    *,
    username_hints: Sequence[str] = DEFAULT_USERNAME_HINTS,
    submit_hints: Sequence[str] = DEFAULT_SUBMIT_HINTS,
) -> Optional[LoginControls]:
    """Match {username, password, submit} by accessible name (pure).

    Password is identified structurally (``input_type=='password'``); the username
    field is the best hint-matched text field (else the field preceding password);
    the submit is the first sign-in-verb button (never an irreversible-verb one).
    Returns ``None`` when any of the three cannot be grounded on THIS screen — the
    single-screen contract kept for back-compat; the multi-screen sequence is
    driven by :meth:`Authenticator.login`.
    """
    password = _match_password_control(controls)
    if password is None:
        # U6 — passwordless member#+PIN: a PIN/passcode text field serves as the
        # secret when the screen has no input_type=password control.
        password = _match_secret_control(controls)
    if password is None:
        return None
    username = _match_username_control(controls, username_hints, password=password)
    if username is None or not _norm(username.get("name")):
        return None
    submit = _match_submit_control(controls, submit_hints)
    if submit is None:
        return None
    return LoginControls(username=dict(username), password=dict(password), submit=dict(submit))


def match_secret_field(
    controls: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """The screen's SECRET control — a password input, or a PIN/passcode text field
    (U6) — or ``None`` when the screen presents no secret.

    A secret is the unambiguous, language-agnostic proof that a screen is a login STEP:
    no public business form asks for a password or PIN. A credential-less crawl that
    reaches one has hit a wall it cannot pass, whether the login is single-screen or the
    password sits on a later screen of a username-first flow.
    """
    return _match_password_control(controls) or _match_secret_control(controls)


def looks_like_signup(controls: Sequence[Mapping[str, Any]]) -> bool:
    """Is this a public REGISTRATION page rather than a login? A signup page legitimately
    asks for a password but is PUBLIC — a credential-less crawl must explore it, never
    stop and mislabel it a login wall. Language-agnostic shapes: a create/register-verb
    submit button, or TWO password fields (password + confirm).
    """
    for c in controls:
        if _norm(c.get("kind")) == "button" and _name_matches_any(
                str(c.get("name")), DEFAULT_SIGNUP_HINTS):
            return True
    return sum(1 for c in controls if _is_password(c)) >= 2


def match_identifier_step(
    controls: Sequence[Mapping[str, Any]],
    *,
    username_hints: Sequence[str] = IDENTIFIER_STEP_HINTS,
    submit_hints: Sequence[str] = DEFAULT_SUBMIT_HINTS,
) -> Optional[Mapping[str, Any]]:
    """A username-first STEP-1 login screen — a HINT-MATCHED identifier field
    (email / username / member#) plus an advance/submit control, and NO secret on this
    screen (the password comes on a later screen). Returns the identifier control, or
    ``None``.

    The identifier MUST be hint-matched (never a bare "search" box), so a public
    single-field form is not mistaken for a login step. Lets a credential-less crawl
    recognise the first step of a multi-step wall and walk to the secret to stop
    honestly, instead of filling synthetic data and looping.
    """
    if match_secret_field(controls) is not None:
        return None
    identifier = next(
        (c for c in _text_fields(controls)
         if _name_matches_any(str(c.get("name")), username_hints)),
        None,
    )
    if identifier is None:
        return None
    if _match_submit_control(controls, submit_hints) is None:
        return None
    return dict(identifier)


def match_otp_control(
    controls: Sequence[Mapping[str, Any]], otp_hints: Sequence[str] = DEFAULT_OTP_HINTS,
) -> Optional[Mapping[str, Any]]:
    """First non-password text-like field whose accessible name is a one-time-code
    hint (the MFA second-factor input).  ``None`` when no OTP field is present."""
    for c in controls:
        if _is_password(c):
            continue
        if _norm(c.get("kind")) not in _TEXT_LIKE_KINDS or not _norm(c.get("name")):
            continue
        if _name_matches_any(str(c.get("name")), otp_hints):
            return dict(c)
    return None


def match_delivery_control(
    controls: Sequence[Mapping[str, Any]],
    *,
    delivery: str,
    delivery_hints: Sequence[str] = DEFAULT_DELIVERY_HINTS,
) -> Optional[Mapping[str, Any]]:
    """The radio/button/link that selects the named OTP delivery channel.

    Matched ONLY when ``delivery`` is set (the profile names email/mobile/…) — so
    the crawler never guesses which channel to trigger.  ``None`` otherwise."""
    d = _norm(delivery)
    if not d:
        return None
    for c in controls:
        if _norm(c.get("kind")) not in ("radio", "button", "link", "checkbox"):
            continue
        if c.get("disabled") or c.get("danger"):
            continue
        name = _norm(c.get("name"))
        if name and d in name:
            return dict(c)
    return None


#: Accessible names of a clickable affordance that NAVIGATES to a login form
#: (rather than a login field). When the login form sits behind a "Sign in"
#: link/button — the near-universal SPA / marketing-front pattern — the crawler
#: clicks this to REACH the login route before authenticating.
_LOGIN_AFFORDANCE_HINTS: tuple[str, ...] = (
    "sign in", "signin", "log in", "login", "log on", "logon", "sign on", "signon",
)


def _match_login_affordance(
    controls: Sequence[Mapping[str, Any]],
) -> Optional[Mapping[str, Any]]:
    """First clickable link/button whose accessible name is a sign-in verb — a
    NAVIGATION to the login form, not a login field. Never an irreversible /
    guard-flagged control. ``None`` when no such affordance exists."""
    for c in controls:
        if _norm(c.get("kind")) not in ("link", "button"):
            continue
        if c.get("disabled") or c.get("danger"):
            continue
        if _name_matches_any(str(c.get("name")), _LOGIN_AFFORDANCE_HINTS):
            return c
    return None


def _index_of(controls: Sequence[Mapping[str, Any]], target: Mapping[str, Any]) -> int:
    for i, c in enumerate(controls):
        if c is target:
            return i
    return len(controls)



def login_screen_is_still_settling(
    *, busy: bool, fingerprint_moved: bool, password_present: bool,
) -> bool:
    """Should the login loop keep waiting before judging the screen?

    TWO SIGNALS, and the order matters because each answers a different failure.

    1. A DISABLED control -- the application's own structural statement that it
       is mid-flight. summit-life-carrier flips its button to "Authenticating..."
       and disables it, which MOVES the fingerprint while saying the answer has
       not arrived; only this signal survives that.

    2. A PASSWORD FIELD still on screen AND a fingerprint that has not moved --
       the application saying, equally structurally, that nothing has happened
       yet. MEASURED (ERPNext v16, 2026-08-28): ERPNext signs in by XHR and
       redirects client-side, disabling nothing while the request is in flight.
       Signal 1 was therefore false on the first look, the loop broke instantly,
       and a login that SUCCEEDED server-side was judged a refusal -- while the
       log said the screen "was given time to" move, which it had not been.

    Deliberately NOT "is there something to submit": summit's page carries "Sign
    in with Google SSO" and "Sign in with Enterprise SSO", so a submit-shaped
    control is present on every observation, busy or not. That was tried and
    fooled. A password FIELD is not a submit BUTTON.

    Both signals are bounded by the same ``LATE_ADVANCE_WAIT_MS`` budget, so a
    genuinely refused login costs at most that before being reported honestly --
    the predicate delays the verdict, it never softens it.
    """
    if busy:
        return True
    return (not fingerprint_moved) and password_present


def verify_login_success(
    *,
    before_fingerprint: str,
    after_fingerprint: str,
    after_controls: Sequence[Mapping[str, Any]],
    after_errors: Sequence[str],
) -> tuple[bool, str]:
    """Grounded login verification (pure). Returns ``(success, reason)``.

    Success requires ALL of: the state fingerprint changed, no password field
    remains, and no error live-region is showing.  Each failing condition yields
    an honest reason — the crawler never treats an unverified login as success.
    """
    if after_fingerprint and after_fingerprint == before_fingerprint:
        return False, "login_unverified: state fingerprint unchanged after submit"
    if any(_is_password(c) for c in after_controls):
        return False, "login_unverified: a password field is still present"
    live_errors = [e for e in after_errors if _norm(e)]
    if live_errors:
        return False, f"login_failed: error region present ({live_errors[0][:120]!r})"
    return True, "login_verified"


# ─── The AUTH-window state machine (caller-enforced guard window) ────────────


class AuthWindow:
    """The ≤N-requests / ≤T-ms window the guard requires around the login POST.

    ``classify_request`` permits the login POST structurally; THIS object is the
    caller-side enforcement the guard docstring defers to (guard.py:508-511).
    The network route handler, while in the AUTH phase, calls :meth:`note` for
    every request and consults :meth:`is_open` before allowing a mutating POST.
    Fail-closed: a window that was never opened, is over budget, or is past its
    deadline is CLOSED, so an AUTH-phase mutation outside the window is blocked.
    """

    def __init__(self, *, max_requests: int, window_ms: int) -> None:
        self.max_requests = max(0, int(max_requests))
        self.window_ms = max(0, int(window_ms))
        self._opened_at_ms: Optional[int] = None
        self._count = 0

    @property
    def opened(self) -> bool:
        return self._opened_at_ms is not None

    @property
    def count(self) -> int:
        return self._count

    def open(self, now_ms: int) -> None:
        """Open the window at the moment the login submit is issued."""
        self._opened_at_ms = int(now_ms)
        self._count = 0

    def note(self, now_ms: int) -> None:
        """Record one request observed during the AUTH phase."""
        if self._opened_at_ms is None:
            self._opened_at_ms = int(now_ms)
        self._count += 1

    def is_open(self, now_ms: int) -> bool:
        """True while the window is within BOTH its request and time budgets."""
        if self._opened_at_ms is None:
            return False
        if self._count > self.max_requests:
            return False
        return (int(now_ms) - self._opened_at_ms) <= self.window_ms

    def close(self) -> None:
        self._opened_at_ms = None
        self._count = 0


# ─── The authenticator ───────────────────────────────────────────────────────


class Authenticator:
    """Drives the login flow through the :class:`BrowserPort` and verifies it.

    Holds the re-login attempt budget; the crawler calls :meth:`login` once up
    front and :meth:`relogin` when it detects session expiry mid-crawl.
    """

    def __init__(
        self,
        port: BrowserPort,
        credentials: Credentials,
        clock: emit.MonotonicClock,
        refuse_pack: Any,
        auth_window: AuthWindow,
        *,
        max_relogins: int = 3,
    ) -> None:
        self._port = port
        self._creds = credentials
        self._clock = clock
        self._refuse_pack = refuse_pack
        # ONE AUTHORITY FOR PAGE IDENTITY. The login flow compares the screen
        # before a submit against the screen after it, which is the same
        # question the crawl asks, so it must be answered by the same code.
        self._identity = StateFingerprinter()
        self._window = auth_window
        self._max_relogins = max(0, int(max_relogins))
        self._relogins = 0

    #: Hard bound on login SCREENS driven per attempt (landing → username →
    #: password → delivery → OTP → done is 5; 6 gives headroom without looping).
    MAX_LOGIN_STEPS = 6

    #: A16 -- how long to keep looking after a submit that appears to have
    #: changed nothing. A login handler may answer LATE: summit-life-carrier's
    #: awaits 1200ms before revealing its MFA step, so the click returns, the
    #: port settles on network idle + hydration (both already quiet -- the work
    #: is a timer, not a request), and the screen the crawl reads is the one it
    #: just submitted. Read once, that is indistinguishable from a form that
    #: refused to advance.
    #:
    #: Paid ONLY on the path that was about to abandon the login entirely, so a
    #: healthy login pays nothing: the fingerprint has already moved and the
    #: loop below breaks on its first pass.
    LATE_ADVANCE_WAIT_MS = 4000
    LATE_ADVANCE_LOOKS = 4

    async def login(self, observation: PageObservation) -> AuthResult:
        """Drive a login SEQUENCE from ``observation`` and verify it.

        A single machine handles every shape of the login ladder:
          * single-step  (username + password + submit on one screen);
          * multi-step   (username → Next → password → Submit);
          * MFA          (… → choose delivery → enter one-time code → Verify),
            where the code is a live TOTP (from the seed) or a fixed test OTP.
        On each screen it fills whatever login fields are present, advances, and
        re-observes — success is VERIFIED (no password/OTP field remains, state
        moved, no error region), never assumed.  Credential-free result.
        """
        controls = build_inventory(observation.raw_controls, self._refuse_pack, url=observation.url)
        before_fp = self._identity.fingerprint(
            url=observation.url, controls=controls, dialogs=observation.dialog_flags)

        # (a) Reach the login form. When NEITHER a username nor a password field is
        # present, the form sits behind a "Sign in" affordance (SPA/marketing
        # front) — click it to load the login route. Bounded to two hops.
        nav_actions: list[emit.ActionRecord] = []
        hops = 0
        while (
            hops < 2
            and _match_password_control(controls) is None
            # HINT-matched, not positional: an unrelated text field on the
            # landing page must not stand in for a login form and suppress the
            # hop. The step loop below keeps the permissive matcher.
            and _hinted_username_control(controls, self._creds.username_hints) is None
        ):
            affordance = _match_login_affordance(controls)
            if affordance is None:
                break
            obs_nav = await self._port.click(dict(affordance))
            nav_actions.append(emit.build_action_record(
                dict(affordance), verb="click", value=None, observation=obs_nav,
                phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
            ))
            observation = await self._observe_current()
            controls = build_inventory(observation.raw_controls, self._refuse_pack, url=observation.url)
            before_fp = self._identity.fingerprint(
                url=observation.url, controls=controls, dialogs=observation.dialog_flags)
            hops += 1

        # (b) Step loop across login screens.
        actions: list[emit.ActionRecord] = list(nav_actions)
        filled_username = False
        filled_delivery = False
        filled_password = False
        filled_otp = False

        for step in range(self.MAX_LOGIN_STEPS):
            controls = build_inventory(observation.raw_controls, self._refuse_pack, url=observation.url)
            screen_fp = self._identity.fingerprint(
                url=observation.url, controls=controls, dialogs=observation.dialog_flags)

            password_ctrl = _match_password_control(controls)
            username_ctrl = _match_username_control(controls, self._creds.username_hints, password=password_ctrl)
            otp_ctrl = match_otp_control(controls, self._creds.otp_hints) if self._creds.mfa else None
            delivery_ctrl = (
                match_delivery_control(controls, delivery=self._creds.mfa.delivery,
                                       delivery_hints=self._creds.delivery_hints)
                if (self._creds.mfa and self._creds.mfa.delivery and not filled_delivery)
                else None
            )
            # Anchor on the form's own field so site chrome cannot supply the
            # submit button — see _match_submit_control.
            submit_ctrl = _match_submit_control(
                controls, self._creds.submit_hints,
                after=(_match_password_control(controls)
                       or _match_username_control(controls,
                                                  self._creds.username_hints)))

            acted = False
            if _auth_identifiable(username_ctrl) and not filled_username:
                obs_u = await self._port.fill(dict(username_ctrl), self._creds.username)
                if obs_u.committed_value is None:
                    # The fill DID NOT TAKE (e.g. the username heuristic grabbed a
                    # non-text control — "Cannot type text into input[type=number]").
                    # Nothing happened on the page, so NOTHING is recorded: a
                    # valueless 'type' action would be a dishonest manifest the
                    # substrate rightly refuses, killing the whole crawl over one
                    # broken fill. Mark it tried (never loop on the same broken
                    # fill); ``acted`` stays False so a submit is never clicked on
                    # the back of a fill that didn't happen.
                    logger.warning(
                        "qec.auth.username_fill_uncommitted control=%r detail=%s",
                        _norm(username_ctrl.get("name")), (obs_u.error_detail or "")[:160],
                    )
                    filled_username = True
                else:
                    actions.append(emit.build_action_record(
                        dict(username_ctrl), verb="type", value=obs_u.committed_value,
                        observation=obs_u, phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
                    ))
                    filled_username = True
                    acted = True
            # A PASSWORD IS TYPED ONCE PER LOGIN SEQUENCE — the same guard the
            # username branch above has carried all along, and its absence here
            # cost an entire application.
            #
            # THE DEFECT (A16, measured on summit-life-carrier). Its sign-in
            # handler awaits 1200ms and THEN calls router.push, so the click
            # returns and the port settles while the navigation is still only
            # scheduled. The next iteration therefore re-derives a password
            # control from a screen the browser is about to leave, re-types a
            # password already committed two iterations earlier, and the fill
            # spends a full 30s action timeout watching the element detach:
            #
            #   fill#2 committed_value='...'   url=/portal/sign-in
            #   fill#4 committed_value=None    url=/dashboard/overview   <-- IN
            #
            # The login had SUCCEEDED. `committed_value is None` was then read as
            # an uncommitted credential, and a crawl that was already signed in
            # ended `stop_reason=auth_failed`, states=1. Nothing about the
            # application was ever discovered.
            #
            # Re-typing buys nothing in any login shape: on a screen that did not
            # advance the "stuck" check below breaks the loop, and a screen that
            # rejected the credential is caught by the live-error branch. It can
            # only ever repeat a secret into a page that has moved on.
            if (_auth_identifiable(password_ctrl)
                    and not filled_password):
                obs_p = await self._port.fill(dict(password_ctrl), self._creds.password)
                if obs_p.committed_value is None:
                    # An uncommitted password fill is NOT recorded and does NOT set
                    # ``filled_password`` — that flag gates the login-success claim,
                    # and claiming a password was typed when the fill errored would
                    # green-wash the verify. Bounded by MAX_LOGIN_STEPS + the
                    # not-acted break, so this can never spin.
                    logger.warning(
                        "qec.auth.password_fill_uncommitted detail=%s",
                        (obs_p.error_detail or "")[:160],
                    )
                else:
                    actions.append(emit.build_action_record(
                        dict(password_ctrl), verb="type", value="", observation=obs_p,
                        phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(), is_secret=True,
                    ))
                    filled_password = True
                    acted = True
            if delivery_ctrl is not None:
                obs_d = await self._port.click(dict(delivery_ctrl))
                actions.append(emit.build_action_record(
                    dict(delivery_ctrl), verb="click", value=None, observation=obs_d,
                    phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
                ))
                filled_delivery = True
                acted = True
            if otp_ctrl is not None and _norm(otp_ctrl.get("name")) and self._creds.mfa is not None:
                code = self._creds.mfa.current_code()
                if code:
                    obs_o = await self._port.fill(dict(otp_ctrl), code)
                    if obs_o.committed_value is None:
                        # Same honesty rule as the password: an uncommitted OTP fill
                        # is neither recorded nor counted as entered.
                        logger.warning(
                            "qec.auth.otp_fill_uncommitted detail=%s",
                            (obs_o.error_detail or "")[:160],
                        )
                    else:
                        actions.append(emit.build_action_record(
                            dict(otp_ctrl), verb="type", value="", observation=obs_o,
                            phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(), is_secret=True,
                        ))
                        filled_otp = True
                        acted = True

            if acted and submit_ctrl is not None:
                # Open the guard AUTH window at the moment of each login POST.
                self._window.open(self._clock.now_ms())
                obs_s = await self._port.click(dict(submit_ctrl))
                actions.append(emit.build_action_record(
                    dict(submit_ctrl), verb="submit", value=None, observation=obs_s,
                    phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
                ))
                self._window.close()
            elif not acted:
                # Nothing to fill or submit on this screen — a dead end.
                break

            observation = await self._observe_current()
            # THE SECOND LOOK (A16). See LATE_ADVANCE_WAIT_MS: a submit whose
            # effect is SCHEDULED rather than immediate leaves the first
            # observation showing the pre-submit screen, and every fact derived
            # below -- has_password, has_otp, after_submit, and the "stuck" break
            # at the bottom of the loop -- then describes a screen that no longer
            # exists. Measured on summit-life-carrier: the crawl declared a
            # two-phase sign-in stuck before its MFA step had rendered, and
            # reported auth_failed on an application it was one click away from
            # being signed into.
            #
            # Bounded and self-limiting: it stops the instant the fingerprint
            # moves, so the only crawls that pay for it are the ones that would
            # otherwise have abandoned the login.
            _looks = max(1, self.LATE_ADVANCE_LOOKS)
            for _look in range(_looks):
                after_controls = build_inventory(
                    observation.raw_controls, self._refuse_pack, url=observation.url)
                after_fp = self._identity.fingerprint(
                    url=observation.url, controls=after_controls,
                    dialogs=observation.dialog_flags)
                if not acted or _look == _looks - 1:
                    break
                # IS THE APPLICATION STILL WORKING?
                #
                # Not "did the fingerprint move" -- that was the first version of
                # this check and summit-life-carrier walked straight through it.
                # Its submit flips the button to "Authenticating..." and DISABLES
                # it, which moves the fingerprint while saying precisely that the
                # answer has not arrived. Read as an advance, the loop then
                # inventories a screen holding a spinner, finds no OTP field, and
                # abandons a login 1200ms from its MFA step.
                #
                # Nor "is there something to submit" -- the second version tried
                # that and was fooled just as fast: this page carries "Sign in
                # with Google SSO" and "Sign in with Enterprise SSO", so a
                # submit-shaped control is present on EVERY observation, busy or
                # not.
                #
                # A DISABLED control is the application's own structural
                # statement that it is mid-flight. It needs no vocabulary, no
                # spinner detection and no page knowledge, and it clears itself
                # the moment the work finishes. Costs a few seconds once on a
                # screen with a permanently disabled button, and only inside the
                # login loop.
                _busy = any(bool(c.get("disabled")) for c in after_controls)
                if not login_screen_is_still_settling(
                        busy=_busy,
                        fingerprint_moved=bool(after_fp and after_fp != screen_fp),
                        password_present=_match_password_control(
                            after_controls) is not None):
                    break
                await asyncio.sleep(
                    self.LATE_ADVANCE_WAIT_MS / 1000.0 / max(1, _looks - 1))
                observation = await self._observe_current()
            if acted and after_fp == screen_fp:
                logger.info(
                    "qec.auth.no_advance_after_submit url=%s looks=%d wait_ms=%d "
                    "- the screen did not move, and was given time to",
                    (observation.url or "")[:120], _looks, self.LATE_ADVANCE_WAIT_MS)
            live_errors = [e for e in observation.error_texts if _norm(e)]
            has_password = _match_password_control(after_controls) is not None
            has_otp = self._creds.mfa is not None and match_otp_control(after_controls, self._creds.otp_hints) is not None
            after_submit = _match_submit_control(after_controls, self._creds.submit_hints) is not None
            after_delivery = bool(
                self._creds.mfa is not None and self._creds.mfa.delivery
                and match_delivery_control(after_controls, delivery=self._creds.mfa.delivery,
                                           delivery_hints=self._creds.delivery_hints) is not None
            )
            # An MFA challenge is still PENDING until the code is entered. A screen
            # with no password/OTP field but a remaining login action (a submit/next
            # or a delivery choice) is an INTERMEDIATE step — e.g. "how do you want
            # your code?" — NOT a completed login. Keep going rather than declare
            # success early (the bug that would green-wash a half-finished MFA login).
            mfa_pending = self._creds.mfa is not None and not filled_otp
            intermediate_mfa_step = mfa_pending and (after_submit or after_delivery)

            # Honest failure: an error live-region after a secret was submitted.
            if live_errors and (filled_password or filled_otp):
                return AuthResult(
                    success=False,
                    reason=f"login_failed: error region present ({live_errors[0][:120]!r})",
                    actions=actions, before_fingerprint=before_fp, after_fingerprint=after_fp,
                    secret_submitted=(filled_password or filled_otp),
                )

            # Success: password entered, no password/OTP field remains, state moved,
            # and no MFA challenge is still mid-flight.
            if filled_password and not has_password and not has_otp and not intermediate_mfa_step:
                success, reason = verify_login_success(
                    before_fingerprint=before_fp, after_fingerprint=after_fp,
                    after_controls=after_controls, after_errors=observation.error_texts,
                )
                if success:
                    storage_state = await self._capture_storage_state()
                    logger.info("qec.auth.login_attempt success=True reason=%s steps=%d", reason, step + 1)
                    return AuthResult(
                        success=True, reason=reason, actions=actions,
                        storage_state=storage_state, before_fingerprint=before_fp,
                        after_fingerprint=after_fp,
                        secret_submitted=(filled_password or filled_otp),
                    )

            # Stuck: we submitted but the screen did not advance and no error/next
            # field appeared → fail fast rather than re-POST the same form.
            if acted and after_fp and after_fp == screen_fp and not has_otp:
                break

        # SAY WHICH ONE. The reason below lists three possibilities, and an
        # operator reading it learns nothing: "username/password/MFA not
        # groundable, OR state did not advance" is a menu, not a diagnosis.
        # Measured on parabank.parasoft.com 2026-09-02: the login failed on
        # every attempt and this was the only output, so which of the three had
        # happened could not be told without attaching a debugger.
        #
        # These are exactly the conditions the success test above reads, so the
        # diagnosis cannot drift from the decision it explains.
        _why = []
        if not filled_username:
            _why.append("no username field could be filled")
        if not filled_password:
            _why.append("no password field could be filled")
        if filled_password and has_password:
            _why.append("the password field is STILL PRESENT after submit - the "
                        "form did not advance (submit not found, not clicked, or refused)")
        if filled_password and not acted:
            _why.append("nothing was clicked after filling - no submit control matched")
        logger.info(
            "qec.auth.login_attempt success=False reason=login_unverified detail=%s",
            "; ".join(_why) or "the sequence ended without a verifiable state change",
        )
        return AuthResult(
            success=False,
            reason="login_unverified: could not complete the login sequence "
                   "(username/password/MFA not groundable, or state did not advance)",
            actions=actions, before_fingerprint=before_fp,
            secret_submitted=(filled_password or filled_otp),
        )

    async def _capture_storage_state(self) -> Optional[dict[str, Any]]:
        """Best-effort session capture (never fails a verified login)."""
        try:
            return await self._port.storage_state()
        except Exception as exc:
            logger.warning("qec.auth.storage_state_capture_failed error=%s", str(exc)[:200])
            return None

    async def relogin(self) -> AuthResult:
        """Re-authenticate on detected session expiry, bounded to the budget."""
        if self._relogins >= self._max_relogins:
            return AuthResult(success=False,
                              reason=f"relogin_budget_exhausted (>{self._max_relogins})")
        self._relogins += 1
        logger.info("qec.auth.relogin attempt=%d/%d", self._relogins, self._max_relogins)
        observation = await self._observe_current()
        return await self.login(observation)

    async def _observe_current(self) -> PageObservation:
        """Snapshot the live page into a :class:`PageObservation`."""
        return PageObservation(
            url=await self._port.current_url(),
            title=await self._port.title(),
            raw_controls=await self._port.collect_controls(),
            dialog_flags=await self._port.dialog_flags(),
            error_texts=await self._port.error_texts(),
        )


# ─── Crawl-level auth outcome vocabulary (M0.3 / T-DE-07) ────────────────────
# Moved here from crawler.py so app.coverage can render the operator-facing
# remediation for each outcome without importing the crawler.  These name WHY
# a crawl's authentication was incomplete or blocked; each maps to a DIFFERENT
# instruction, and confusing them sends the operator after the wrong artefact.

#: revoked). Sessions are captured once and reused for every later crawl, so this
#: is the STEADY STATE of any app crawled more than a session-lifetime apart, not
#: an edge case. Detected structurally (a password field on the entry screen), so
#: it holds for any app in any language — never a URL or copy match.
AUTH_SESSION_EXPIRED = "session_expired"

#: ``auth_blocked`` reason: the entry sits behind a login wall and NEITHER
#: credentials NOR a session were supplied — the crawl had no way to sign in, so it
#: stopped at the wall (``STOP_AUTH_REQUIRED``). Distinct from AUTH_SESSION_EXPIRED
#: (a session was injected but died) and from the credentials-supplied-but-no-form
#: case: the remediation is "record a login / attach credentials", never "re-record".
AUTH_NO_CREDENTIALS = "no_credentials"

#: ``auth_incomplete`` reason: the crawl HELD a verified login — it signed in
#: successfully — and the app still answered a fresh page load with its sign-in screen.
#: The app keeps the signed-in user in CLIENT-SIDE state rather than a cookie, so every
#: navigation drops it. Neither re-recording nor new credentials can fix that (both were
#: already proven to work), which is why it must never wear the session_expired advice.
AUTH_NOT_PERSISTED = "not_persisted"
