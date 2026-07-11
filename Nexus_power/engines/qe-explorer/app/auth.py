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

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from . import emit
from .browser import BrowserPort, PageObservation, RawObservation
from .fingerprint import state_fingerprint
from .guard import Phase, classify_action_verb
from .inventory import build_inventory

logger = logging.getLogger(__name__)

#: Default accessible-name hints for the USERNAME field (config/data-driven —
#: extend via the explore request, never hard-code a client's labels).
DEFAULT_USERNAME_HINTS: tuple[str, ...] = (
    "username", "user name", "user id", "userid", "email", "e-mail",
    "login", "account", "member id", "user",
)
#: Default accessible-name hints for the SUBMIT control.
DEFAULT_SUBMIT_HINTS: tuple[str, ...] = (
    "sign in", "signin", "log in", "login", "log on", "logon",
    "continue", "submit", "next", "go", "enter",
)

_PASSWORD_INPUT_TYPES = frozenset({"password"})


@dataclass(frozen=True)
class Credentials:
    """Login secret + optional accessible-name hints (all data-driven)."""

    username: str
    password: str
    username_hints: tuple[str, ...] = DEFAULT_USERNAME_HINTS
    submit_hints: tuple[str, ...] = DEFAULT_SUBMIT_HINTS

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]]) -> "Optional[Credentials]":
        """Build from the explore-request ``credentials`` object, or ``None``."""
        if not payload:
            return None
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        if not username or not password:
            return None
        uh = tuple(str(h).strip().lower() for h in (payload.get("username_hints") or ()) if str(h).strip())
        sh = tuple(str(h).strip().lower() for h in (payload.get("submit_hints") or ()) if str(h).strip())
        return cls(
            username=username,
            password=password,
            username_hints=uh or DEFAULT_USERNAME_HINTS,
            submit_hints=sh or DEFAULT_SUBMIT_HINTS,
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


def match_login_controls(
    controls: Sequence[Mapping[str, Any]],
    *,
    username_hints: Sequence[str] = DEFAULT_USERNAME_HINTS,
    submit_hints: Sequence[str] = DEFAULT_SUBMIT_HINTS,
) -> Optional[LoginControls]:
    """Match {username, password, submit} by accessible name (pure).

    Password is identified structurally (``input_type=='password'``) — the
    single reliable, locale-independent signal.  The username field is the
    best hint-matched text field, falling back to the text field immediately
    preceding the password field (the near-universal login layout).  The submit
    control is the first button whose name is a sign-in verb; a button whose
    name matches an irreversible refuse verb is never chosen.  Returns ``None``
    when any of the three cannot be grounded — the caller then refuses to guess.
    """
    password = next((c for c in controls if _is_password(c) and _norm(c.get("name"))), None)
    if password is None:
        # A nameless password field still anchors the form; accept it for the
        # username-precedes-password heuristic but it cannot be a fill target.
        password = next((c for c in controls if _is_password(c)), None)
    if password is None:
        return None

    text_fields = [
        c for c in controls
        if _norm(c.get("kind")) in ("text", "date", "select") and not _is_password(c)
        and _norm(c.get("name"))
    ]
    username = next(
        (c for c in text_fields if _name_matches_any(str(c.get("name")), username_hints)),
        None,
    )
    if username is None:
        # Fallback: the last named text field appearing before the password.
        try:
            p_idx = list(controls).index(password)
        except ValueError:
            p_idx = len(controls)
        preceding = [c for c in text_fields if _index_of(controls, c) < p_idx]
        username = preceding[-1] if preceding else (text_fields[0] if text_fields else None)
    if username is None or not _norm(username.get("name")):
        return None

    submit = None
    for c in controls:
        if _norm(c.get("kind")) != "button" or not _norm(c.get("name")):
            continue
        if c.get("danger"):  # never choose an irreversible-verb button as submit
            continue
        if _name_matches_any(str(c.get("name")), submit_hints):
            submit = c
            break
    if submit is None:
        return None
    return LoginControls(username=dict(username), password=dict(password), submit=dict(submit))


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
        self._window = auth_window
        self._max_relogins = max(0, int(max_relogins))
        self._relogins = 0

    async def login(self, observation: PageObservation) -> AuthResult:
        """Attempt one inventory-matched login from ``observation`` (the login
        page as first seen).  Returns a grounded, credential-free result.
        """
        controls = build_inventory(observation.raw_controls, self._refuse_pack,
                                   url=observation.url)
        before_fp = state_fingerprint(observation.url, controls, observation.dialog_flags)
        matched = match_login_controls(
            controls,
            username_hints=self._creds.username_hints,
            submit_hints=self._creds.submit_hints,
        )

        # The login form may sit BEHIND a "Sign in" affordance (a link/button)
        # rather than on the entry/landing page — the near-universal SPA pattern.
        # When the login controls are not groundable here, click a login affordance
        # to load the login route, re-inventory, and re-match. Bounded to two hops.
        nav_actions: list[emit.ActionRecord] = []
        hops = 0
        while matched is None and hops < 2:
            affordance = _match_login_affordance(controls)
            if affordance is None:
                break
            obs_nav = await self._port.click(dict(affordance))
            nav_actions.append(emit.build_action_record(
                dict(affordance), verb="click", value=None, observation=obs_nav,
                phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
            ))
            observation = await self._observe_current()
            controls = build_inventory(observation.raw_controls, self._refuse_pack,
                                       url=observation.url)
            before_fp = state_fingerprint(observation.url, controls, observation.dialog_flags)
            matched = match_login_controls(
                controls,
                username_hints=self._creds.username_hints,
                submit_hints=self._creds.submit_hints,
            )
            hops += 1

        if matched is None:
            logger.info("qec.auth.login_controls_unmatched url_host=%s",
                        _norm(observation.title) and "<redacted>" or "")
            return AuthResult(success=False,
                              reason="login_unmatched: could not ground "
                                     "username/password/submit by accessible name",
                              before_fingerprint=before_fp)

        actions: list[emit.ActionRecord] = list(nav_actions)
        # 1. username (PII-scrubbed at source — never raw at rest)
        obs_u = await self._port.fill(matched.username, self._creds.username)
        actions.append(emit.build_action_record(
            matched.username, verb="type", value=obs_u.committed_value,
            observation=obs_u, phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
        ))
        # 2. password (value EMPTIED at source; guard never sees it either)
        obs_p = await self._port.fill(matched.password, self._creds.password)
        actions.append(emit.build_action_record(
            matched.password, verb="type", value="", observation=obs_p,
            phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(), is_secret=True,
        ))
        # 3. submit — open the AUTH window at the moment of the login POST
        self._window.open(self._clock.now_ms())
        obs_s = await self._port.click(matched.submit)
        actions.append(emit.build_action_record(
            matched.submit, verb="submit", value=None, observation=obs_s,
            phase=Phase.AUTH.value, timestamp_ms=self._clock.now_ms(),
        ))

        after_obs = await self._observe_current()
        after_controls = build_inventory(after_obs.raw_controls, self._refuse_pack,
                                         url=after_obs.url)
        after_fp = state_fingerprint(after_obs.url, after_controls, after_obs.dialog_flags)
        success, reason = verify_login_success(
            before_fingerprint=before_fp,
            after_fingerprint=after_fp,
            after_controls=after_controls,
            after_errors=after_obs.error_texts,
        )

        storage_state: Optional[dict[str, Any]] = None
        if success:
            try:
                storage_state = await self._port.storage_state()
            except Exception as exc:  # capture is best-effort; login still succeeded
                logger.warning("qec.auth.storage_state_capture_failed error=%s",
                               str(exc)[:200])
        self._window.close()
        logger.info("qec.auth.login_attempt success=%s reason=%s", success, reason)
        return AuthResult(
            success=success, reason=reason, actions=actions,
            storage_state=storage_state, before_fingerprint=before_fp,
            after_fingerprint=after_fp,
        )

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
