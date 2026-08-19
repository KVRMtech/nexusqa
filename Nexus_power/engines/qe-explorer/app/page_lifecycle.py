"""Browser page lifecycle — the POLICY half of M1.5 (T-ND-01 … T-ND-04).

WHAT WAS MISSING, MEASURED.  A crawl created exactly ONE page and attached two
listeners to it (``response`` and ``websocket``).  There was no
``context.on("page")`` and no ``page.on("dialog")``, and four whole classes of
real application behaviour fell through that gap:

  * a native ``confirm()`` was AUTO-DISMISSED by Playwright — its documented
    behaviour when a page has no ``dialog`` listener — so a confirm-gated
    "Continue" silently cancelled and the funnel recorded an honest-looking
    "nothing happened";
  * a ``target="_blank"`` / ``window.open()`` step created a second page the
    crawler never learned about, so the walk went on acting against, and
    fingerprinting, the page it had already left;
  * a download produced no artifact at all;
  * a page swap left state identity pointing at the stale page.

THIS MODULE DECIDES; IT DOES NOT DRIVE.  Everything here is pure: no Playwright
import, no I/O, no clock, no randomness.  It takes what was OBSERVED about a
dialog / a popup / a download and returns a decision plus the reason for it, so
the decision is unit-testable without a browser and auditable after the fact.
:mod:`app.playwright_port` owns the live objects and calls in here for every
choice it makes.  That is the same split :mod:`app.browser` already uses for
``classify_after`` / ``verify_intent``, and for the same reason.

THE LIFECYCLE, NAMED::

    CREATED ──▶ OBSERVED ──▶ ACTIVE ──▶ SWAPPED ──▶ CLOSED
                    │                       │
                    └──────▶ RETAINED ◀─────┘

  CREATED   the context reported a new page; nothing is known about it yet.
  OBSERVED  it reached a usable load state and its URL was read.
  ACTIVE    it is THE journey page: actions, fingerprints and evidence use it.
  SWAPPED   it was active and something else has taken over.
  RETAINED  observed, never adopted (or adopted and superseded) — still open,
            recorded as evidence, and NOT authoritative for anything.
  CLOSED    the page is gone.

Exactly one page is ACTIVE at any moment (:meth:`PageRegistry.active`), which is
what keeps "which page is authoritative" from becoming an ambiguous global.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

# ─── Lifecycle states ────────────────────────────────────────────────────────

LIFECYCLE_CREATED = "created"
LIFECYCLE_OBSERVED = "observed"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_SWAPPED = "swapped"
LIFECYCLE_RETAINED = "retained"
LIFECYCLE_CLOSED = "closed"

#: Every legal lifecycle state, in the order the diagram above walks them.
LIFECYCLE_STATES = (
    LIFECYCLE_CREATED, LIFECYCLE_OBSERVED, LIFECYCLE_ACTIVE,
    LIFECYCLE_SWAPPED, LIFECYCLE_RETAINED, LIFECYCLE_CLOSED,
)

# ─── Dialog intent classifications ───────────────────────────────────────────

#: The dialog asks the user to confirm the very step the walk is taking.
#: Accepting it is what lets the journey advance; auto-dismissal is what
#: silently cancelled it before M1.5.
INTENT_FUNNEL_CONFIRMATION = "funnel_confirmation"
#: "Are you sure you want to leave this page?" — the browser (or the app)
#: warning that navigating away discards work.  Answering YES abandons the flow.
INTENT_LEAVE_WARNING = "leave_warning"
#: "Delete this policy permanently?" — a native confirm guarding an
#: irreversible act.  A native confirm is NOT an approved crossing; the
#: approval subsystem (A4.3) is, and it speaks through ``approved_labels``.
INTENT_DESTRUCTIVE_CONFIRMATION = "destructive_confirmation"
#: ``alert()`` — one button, nothing to decide, but it BLOCKS the page until it
#: is answered, so it must still be answered and recorded.
INTENT_NOTICE = "notice"
#: ``prompt()`` — asks for free text.  There is no grounded value to supply, and
#: inventing one is fabricating user input, so it is declined.
INTENT_PROMPT_UNANSWERABLE = "prompt_unanswerable"

ACTION_ACCEPT = "accept"
ACTION_DISMISS = "dismiss"

#: Dialog types Playwright reports.  ``beforeunload`` is the browser's own
#: navigation warning and is never an application confirm.
DIALOG_ALERT = "alert"
DIALOG_CONFIRM = "confirm"
DIALOG_PROMPT = "prompt"
DIALOG_BEFOREUNLOAD = "beforeunload"

#: Message fragments that make a dialog a LEAVE warning.  Deliberately narrow:
#: every one of these is about departing the page, not about the step itself.
_LEAVE_LEXICON = (
    "leave this page", "leave site", "leave the page", "reload site",
    "changes you made may not be saved", "changes may not be saved",
    "unsaved changes", "your changes will be lost", "changes will be lost",
    "discard your changes", "navigate away", "exit without saving",
    "lose your progress", "progress will be lost",
)

#: Message fragments that make a dialog DESTRUCTIVE.  A crawl never answers YES
#: to one of these on message text alone.
_DESTRUCTIVE_LEXICON = (
    "delete", "permanently remove", "remove permanently", "erase",
    "cannot be undone", "can not be undone", "cant be undone",
    "withdraw your application", "cancel your application",
    "cancel this application", "discard this application", "terminate",
    "close your account", "deactivate",
)

#: Control labels that mark the click as a FORWARD step in a funnel.  Used only
#: to disambiguate a confirm whose own message says nothing decisive.
_ADVANCE_LABELS = (
    "continue", "next", "submit", "proceed", "confirm", "agree", "accept",
    "apply", "save", "finish", "complete", "yes", "start", "sign", "send",
)

#: Control labels that mark the click as destructive even when the message is
#: bland ("Are you sure?" behind a Delete button).
_DESTRUCTIVE_LABELS = (
    "delete", "remove", "erase", "destroy", "wipe", "purge", "revoke",
    "deactivate", "terminate", "withdraw", "discard",
)


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _hits(haystack: str, needles: Sequence[str]) -> str:
    """The FIRST needle present in ``haystack``, or ``""`` — returned rather
    than a bool so the reason string can name what actually matched."""
    for needle in needles:
        if needle in haystack:
            return needle
    return ""


@dataclass(frozen=True)
class DialogDecision:
    """What to do about ONE native dialog, and why.

    ``action`` is what the adapter executes; ``intent`` and ``reason`` are what
    the evidence record carries.  A decision no one can explain afterwards is
    not auditable, so the reason is part of the return value rather than a log
    line that may or may not have been emitted.
    """

    action: str
    intent: str
    reason: str

    @property
    def accepted(self) -> bool:
        return self.action == ACTION_ACCEPT


def resolve_dialog(
    *,
    dialog_type: str,
    message: str,
    control_label: str = "",
    action_verb: str = "",
    journey_phase: str = "",
    observe_only: bool = False,
    approved_labels: Sequence[str] = (),
) -> DialogDecision:
    """Decide accept-or-dismiss for one native dialog.  PURE.

    THE PRECEDENCE, checked in exactly this order:

      1. ``beforeunload`` — the browser's own leave warning.  DISMISS.  In
         Playwright's semantics ``accept()`` means *leave the page*; a walk that
         accepted one would abandon the journey it is standing in the middle of.
      2. ``prompt`` — DISMISS.  There is no grounded value to type, and a
         fabricated one is invented user input.
      3. a LEAVE message on any dialog type — DISMISS, for the reason in (1).
         Checked before (4) because "discard your changes and leave?" is a
         leave warning first and a destructive-sounding string second.
      4. a DESTRUCTIVE message, or a destructive control label — DISMISS,
         UNLESS the control carries an explicit operator approval, in which
         case the approval is the authority and the dialog is accepted.  A
         native ``confirm()`` is not an approval; A4.3's grant is.
      5. ``alert`` — ACCEPT.  One button; the page stays blocked until it is
         answered, so "acknowledge and record" is the only honest option.
      6. observe-only posture — DISMISS.  A confirm that is not a leave warning
         is gating a mutation, and an observe-only crawl may not commit one.
      7. otherwise — ACCEPT.  This is the funnel confirmation, and accepting it
         is the whole point of T-ND-02.

    Args:
        dialog_type: Playwright's ``dialog.type`` (alert/confirm/prompt/
            beforeunload).  An unknown type is treated as a confirm.
        message: ``dialog.message``, raw.  Callers scrub before recording; the
            decision reads the real text.
        control_label: the accessible name of the control whose click raised
            the dialog, when the adapter knows it.
        action_verb: what the crawl was doing (``click`` / ``fill`` / ``select``
            / …) — carried into evidence and available to future rules.
        journey_phase: the guard phase (explore/auth/walk/submit) — carried
            into evidence and named in the approval reason, so a grant that
            fired in the wrong phase is visible after the fact.
        observe_only: the resolved M0.5 posture.  Raises the floor; never
            lowers it.
        approved_labels: control labels the operator explicitly approved for a
            crossing (A4.3).  Matched case-insensitively against
            ``control_label``; ``"*"`` is a blanket approval.
    """
    dtype = _norm(dialog_type) or DIALOG_CONFIRM
    text = _norm(message)
    label = _norm(control_label)
    phase = _norm(journey_phase)

    if dtype == DIALOG_BEFOREUNLOAD:
        return DialogDecision(
            ACTION_DISMISS, INTENT_LEAVE_WARNING,
            "beforeunload: accepting would leave the page mid-journey")

    if dtype == DIALOG_PROMPT:
        return DialogDecision(
            ACTION_DISMISS, INTENT_PROMPT_UNANSWERABLE,
            "prompt: no grounded value to supply; a fabricated one is invented input")

    leave_hit = _hits(text, _LEAVE_LEXICON)
    if leave_hit:
        return DialogDecision(
            ACTION_DISMISS, INTENT_LEAVE_WARNING,
            f"leave warning matched {leave_hit!r}: staying keeps the journey alive")

    destructive_hit = _hits(text, _DESTRUCTIVE_LEXICON) or _hits(label, _DESTRUCTIVE_LABELS)
    if destructive_hit:
        approved = _approval_for(label, approved_labels)
        if approved and not observe_only:
            return DialogDecision(
                ACTION_ACCEPT, INTENT_DESTRUCTIVE_CONFIRMATION,
                f"destructive confirm matched {destructive_hit!r}; accepted under "
                f"operator approval {approved!r} in phase {phase or 'unknown'}")
        return DialogDecision(
            ACTION_DISMISS, INTENT_DESTRUCTIVE_CONFIRMATION,
            f"destructive confirm matched {destructive_hit!r} with no operator "
            f"approval: a native confirm is not an approved crossing")

    if dtype == DIALOG_ALERT:
        return DialogDecision(
            ACTION_ACCEPT, INTENT_NOTICE,
            "alert: one button, and the page stays blocked until it is answered")

    if observe_only:
        return DialogDecision(
            ACTION_DISMISS, INTENT_FUNNEL_CONFIRMATION,
            "observe-only posture: this confirm gates a mutation that may not be committed")

    advance_hit = _hits(label, _ADVANCE_LABELS)
    reason = (f"funnel confirmation behind {str(control_label).strip()[:60]!r} "
              f"(matched {advance_hit!r})" if advance_hit
              else "funnel confirmation: nothing marks it as a leave warning or destructive")
    return DialogDecision(ACTION_ACCEPT, INTENT_FUNNEL_CONFIRMATION,
                          reason + f"; accepting advances the {action_verb or 'action'}")


def _approval_for(label: str, approved_labels: Sequence[str]) -> str:
    """The approval entry covering ``label`` (``"*"`` is blanket), or ``""``."""
    for entry in approved_labels or ():
        raw = str(entry or "").strip()
        if raw == "*":
            return "*"
        if raw and _norm(raw) == label:
            return raw
    return ""


# ─── Popup adoption ──────────────────────────────────────────────────────────

ADOPT = "adopt"
RETAIN = "retain"

#: Why a popup was not adopted.  Each is recorded verbatim in the evidence.
POPUP_CLOSED = "closed_before_observation"
POPUP_BLANK = "never_navigated"
POPUP_OUT_OF_SCOPE = "out_of_scope"
POPUP_SUPERSEDED = "superseded_by_earlier_popup"

#: URLs a page carries before it has navigated anywhere.
_BLANK_URLS = frozenset({"", "about:blank", "about:blank#blocked", "chrome://newtab/"})


def is_blank_url(url: Any) -> bool:
    """True when ``url`` is a page that has not navigated anywhere yet.

    Public because the adapter needs the same test to decide whether to keep
    waiting for a ``window.open()`` popup to navigate — one definition of
    "blank", used by both the waiter and the adoption policy."""
    return _norm(url) in _BLANK_URLS


@dataclass(frozen=True)
class PopupDecision:
    """Adopt this new page as the journey page, or retain it as evidence only."""

    disposition: str
    reason: str

    @property
    def adopt(self) -> bool:
        return self.disposition == ADOPT


def resolve_popup(
    *,
    popup_url: str,
    opener_url: str = "",
    in_scope: bool = True,
    closed: bool = False,
    already_adopted_this_batch: bool = False,
) -> PopupDecision:
    """Decide whether a newly created page becomes the ACTIVE journey page.  PURE.

    THE POLICY, in order:

      1. the page CLOSED before it could be observed — retain the record, never
         adopt a dead handle;
      2. it never navigated (still ``about:blank`` after the bounded settle) —
         retain.  Adopting a blank page would hand the walk an empty inventory
         and a fingerprint of nothing;
      3. it landed OUT OF SCOPE — retain.  This is the same gate ``_expand``
         applies to a redirect: a third-party page (an IdP, a help centre, a
         payment processor) must never be inventoried as the application's own
         substrate;
      4. an EARLIER popup from the same ACTION was already adopted — retain.
         FIRST usable in-scope popup wins, deterministically: it is the one the
         click produced first, and later ones from the same click are
         overwhelmingly ancillary.  Every one of them is still recorded, with
         the reason it lost;
      5. otherwise — ADOPT.

    ``already_adopted_this_batch`` is scoped by the CALLER, and the adapter
    scopes it to one action rather than to one adjudication pass: a single click
    is adjudicated on both sides of the settle quiesce, so a per-pass rule would
    mean two windows from one click both take over and which one ended up active
    would depend on which happened to load faster.  A popup opened by a LATER
    action is a later move of the journey and is adopted on its own merits —
    otherwise a walk would be stuck on whichever tab it entered first.

    ``opener_url`` is carried for the evidence record and the reason text; it
    deliberately does not gate the decision, because a popup that navigates
    itself away from its opener's origin is still the journey (that is what an
    SSO or an e-sign hand-off looks like) as long as it stays in scope.
    """
    url = str(popup_url or "").strip()
    if closed:
        return PopupDecision(RETAIN, f"{POPUP_CLOSED}: the popup was gone before it settled")
    if _norm(url) in _BLANK_URLS:
        return PopupDecision(RETAIN, f"{POPUP_BLANK}: still blank after the settle budget")
    if not in_scope:
        return PopupDecision(
            RETAIN, f"{POPUP_OUT_OF_SCOPE}: {url[:120]} is not part of the application "
                    f"under test (opener {opener_url[:80]})")
    if already_adopted_this_batch:
        return PopupDecision(
            RETAIN, f"{POPUP_SUPERSEDED}: an earlier popup in this batch is already active")
    return PopupDecision(
        ADOPT, f"popup opened from {opener_url[:80] or '(unknown opener)'} and settled "
               f"on {url[:120]}")


# ─── The registry ────────────────────────────────────────────────────────────


@dataclass
class PageEntry:
    """One page's bookkeeping.  ``handle`` is opaque here — the registry never
    calls a method on it, which is what keeps this module Playwright-free."""

    token: str
    handle: Any
    lifecycle: str = LIFECYCLE_CREATED
    url: str = ""
    opener_url: str = ""
    #: WHICH page opened this one. "" is the primary page, so a popup opened by
    #: an already-adopted tab records its real parent rather than defaulting to
    #: the page the crawl started on — a chain of hand-offs (quote -> e-sign ->
    #: receipt) is only reconstructable if each link names the previous one.
    opener_token: str = ""
    is_primary: bool = False
    reason: str = ""


class PageRegistry:
    """Which pages exist, which ONE is active, and how each got there.

    Deliberately not a set of module globals: one registry per browser port, so
    two crawls in one process share nothing.  The registry answers the lifecycle
    questions the milestone asks — which page is active, what happened to the
    ones that are not — from recorded transitions rather than from whatever
    handle a caller happens to be holding.
    """

    #: Tokens are assigned in creation order.  The PRIMARY page (the one the
    #: crawl started with) deliberately gets the EMPTY token: it is the identity
    #: every fingerprint ever recorded was computed under, and giving it a
    #: non-empty token would move every digest in the corpus.  Only an ADOPTED
    #: page carries one, so a crawl that never adopts is byte-identical to
    #: before M1.5.
    PRIMARY_TOKEN = ""

    def __init__(self) -> None:
        self._entries: list[PageEntry] = []
        self._by_id: dict[int, PageEntry] = {}
        self._active_id: int = 0
        self._minted = 0

    # -- registration ---------------------------------------------------------

    def register_primary(self, handle: Any, *, url: str = "") -> PageEntry:
        entry = PageEntry(token=self.PRIMARY_TOKEN, handle=handle,
                          lifecycle=LIFECYCLE_ACTIVE, url=url, is_primary=True,
                          reason="the page the crawl was started with")
        self._entries.append(entry)
        self._by_id[id(handle)] = entry
        self._active_id = id(handle)
        return entry

    def register(self, handle: Any, *, opener_url: str = "",
                 opener_token: str = "") -> PageEntry:
        """Record a newly CREATED page and mint its token (idempotent)."""
        existing = self._by_id.get(id(handle))
        if existing is not None:
            return existing
        self._minted += 1
        entry = PageEntry(token=f"p{self._minted}", handle=handle,
                          lifecycle=LIFECYCLE_CREATED, opener_url=opener_url,
                          opener_token=opener_token)
        self._entries.append(entry)
        self._by_id[id(handle)] = entry
        return entry

    def get(self, handle: Any) -> Optional[PageEntry]:
        return self._by_id.get(id(handle))

    # -- transitions ----------------------------------------------------------

    def observe(self, handle: Any, *, url: str) -> Optional[PageEntry]:
        entry = self._by_id.get(id(handle))
        if entry is None:
            return None
        entry.url = url
        if entry.lifecycle == LIFECYCLE_CREATED:
            entry.lifecycle = LIFECYCLE_OBSERVED
        return entry

    def adopt(self, handle: Any, *, reason: str = "") -> Optional[PageEntry]:
        """Make ``handle`` THE active page; the outgoing one becomes SWAPPED."""
        entry = self._by_id.get(id(handle))
        if entry is None or entry.lifecycle == LIFECYCLE_CLOSED:
            return None
        outgoing = self._by_id.get(self._active_id)
        if outgoing is not None and outgoing is not entry:
            outgoing.lifecycle = LIFECYCLE_SWAPPED
        entry.lifecycle = LIFECYCLE_ACTIVE
        entry.reason = reason or entry.reason
        self._active_id = id(handle)
        return entry

    def retain(self, handle: Any, *, reason: str = "") -> Optional[PageEntry]:
        entry = self._by_id.get(id(handle))
        if entry is None or entry.lifecycle in (LIFECYCLE_CLOSED, LIFECYCLE_ACTIVE):
            return entry
        entry.lifecycle = LIFECYCLE_RETAINED
        entry.reason = reason or entry.reason
        return entry

    def close(self, handle: Any) -> Optional[PageEntry]:
        entry = self._by_id.get(id(handle))
        if entry is None:
            return None
        entry.lifecycle = LIFECYCLE_CLOSED
        if id(handle) == self._active_id:
            self._active_id = 0          # nothing is active until one is promoted
        return entry

    # -- queries --------------------------------------------------------------

    def active(self) -> Optional[PageEntry]:
        return self._by_id.get(self._active_id)

    def active_token(self) -> str:
        entry = self.active()
        return entry.token if entry is not None else self.PRIMARY_TOKEN

    def has_active(self) -> bool:
        return self.active() is not None

    def candidates_for_promotion(self) -> list[PageEntry]:
        """Open, non-active pages, newest first — who inherits when the active
        page closes.  Newest first because the most recently opened page is the
        one the application most recently intended the user to be looking at."""
        return [e for e in reversed(self._entries)
                if e.lifecycle != LIFECYCLE_CLOSED
                and id(e.handle) != self._active_id]

    def entries(self) -> list[PageEntry]:
        return list(self._entries)

    def open_count(self) -> int:
        return sum(1 for e in self._entries if e.lifecycle != LIFECYCLE_CLOSED)

    def snapshot(self) -> list[dict[str, str]]:
        """The whole registry as evidence-shaped, all-string rows."""
        return [{
            "token": e.token or "primary",
            "lifecycle": e.lifecycle,
            "url": (e.url or "")[:500],
            "opener_url": (e.opener_url or "")[:500],
            "opener_token": e.opener_token or "primary",
            "reason": (e.reason or "")[:300],
        } for e in self._entries]


# ─── Evidence records ────────────────────────────────────────────────────────

EVENT_POPUP = "popup"
EVENT_DIALOG = "dialog"
EVENT_DOWNLOAD = "download"
EVENT_PAGE_CLOSED = "page_closed"


def popup_record(*, opener_url: str, popup_url: str, token: str,
                 decision: PopupDecision, timestamp_ms: int,
                 opener_token: str = "", trigger_label: str = "") -> dict[str, Any]:
    """T-ND-01 evidence: source page, new page, URL, adoption decision, time."""
    return {
        "event": EVENT_POPUP,
        "opener_url": (opener_url or "")[:2000],
        "opener_token": opener_token or "primary",
        "popup_url": (popup_url or "")[:2000],
        "page_token": token or "primary",
        "disposition": decision.disposition,
        "adopted": decision.adopt,
        "reason": (decision.reason or "")[:500],
        "trigger_label": (trigger_label or "")[:200],
        "timestamp_ms": int(timestamp_ms),
    }


def dialog_record(*, dialog_type: str, message: str, decision: DialogDecision,
                  timestamp_ms: int, page_url: str = "", page_token: str = "",
                  control_label: str = "", action_verb: str = "",
                  journey_phase: str = "", handled: bool = True,
                  error: str = "") -> dict[str, Any]:
    """T-ND-02 evidence: type, message, accept/dismiss, reason, page/state."""
    return {
        "event": EVENT_DIALOG,
        "dialog_type": (dialog_type or "")[:40],
        "message": (message or "")[:500],
        "action": decision.action,
        "intent": decision.intent,
        "reason": (decision.reason or "")[:500],
        "page_url": (page_url or "")[:2000],
        "page_token": page_token or "primary",
        "control_label": (control_label or "")[:200],
        "action_verb": (action_verb or "")[:40],
        "journey_phase": (journey_phase or "")[:20],
        "handled": bool(handled),
        "error": (error or "")[:300],
        "timestamp_ms": int(timestamp_ms),
    }


def download_record(*, suggested_filename: str, source_url: str, page_url: str,
                    artifact_path: str, bytes_written: int, timestamp_ms: int,
                    page_token: str = "", content_type: str = "",
                    trigger_label: str = "", action_verb: str = "",
                    error: str = "") -> dict[str, Any]:
    """T-ND-03 evidence: filename, source page, triggering action, artifact
    reference, content type when available.

    ``bytes`` is part of the record on purpose.  "a download happened" and "a
    file exists" are different claims, and only the second one is evidence; a
    zero-byte artifact is reported as zero rather than as success.
    """
    return {
        "event": EVENT_DOWNLOAD,
        "filename": (suggested_filename or "")[:300],
        "source_url": (source_url or "")[:2000],
        "page_url": (page_url or "")[:2000],
        "page_token": page_token or "primary",
        "artifact_path": (artifact_path or "")[:500],
        "bytes": int(bytes_written),
        "content_type": (content_type or "")[:120],
        "trigger_label": (trigger_label or "")[:200],
        "action_verb": (action_verb or "")[:40],
        "captured": bool(artifact_path and bytes_written > 0),
        "error": (error or "")[:300],
        "timestamp_ms": int(timestamp_ms),
    }


def page_closed_record(*, page_url: str, page_token: str, was_active: bool,
                       promoted_token: str, promoted_url: str,
                       timestamp_ms: int) -> dict[str, Any]:
    """Evidence for a page leaving the journey — and who inherited it."""
    return {
        "event": EVENT_PAGE_CLOSED,
        "page_url": (page_url or "")[:2000],
        "page_token": page_token or "primary",
        "was_active": bool(was_active),
        "promoted_token": promoted_token or "",
        "promoted_url": (promoted_url or "")[:2000],
        "timestamp_ms": int(timestamp_ms),
    }


CONTENT_TYPE_BY_SUFFIX: Mapping[str, str] = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".xml": "application/xml",
    ".zip": "application/zip",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

#: Characters that may appear in a stored artifact filename.  A download's
#: ``suggested_filename`` is APPLICATION-CONTROLLED text, so it is never used as
#: a path component without being reduced to this alphabet — a suggested name of
#: ``../../etc/passwd`` must land as a file in the artifact directory, not
#: outside it.
_SAFE_NAME = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def safe_artifact_name(suggested: str, *, index: int) -> str:
    """A filesystem-safe, collision-free artifact name derived from ``suggested``.

    Path separators, traversal segments and every other character outside a
    conservative alphabet are replaced; the result is prefixed with a monotonic
    index so two downloads named ``report.pdf`` cannot overwrite one another —
    which would silently destroy the first artifact and leave the manifest
    claiming both were captured.
    """
    raw = str(suggested or "").strip().replace("\\", "/").split("/")[-1]
    cleaned = "".join(ch if ch in _SAFE_NAME else "_" for ch in raw).strip("._-")
    if not cleaned:
        cleaned = "download"
    return f"{int(index):03d}_{cleaned[:100]}"


def content_type_for(filename: str, declared: str = "") -> str:
    """The download's content type: what the server declared, else the suffix."""
    if str(declared or "").strip():
        return str(declared).split(";", 1)[0].strip()[:120]
    name = str(filename or "").lower()
    for suffix, mime in CONTENT_TYPE_BY_SUFFIX.items():
        if name.endswith(suffix):
            return mime
    return ""


__all__ = [
    "LIFECYCLE_CREATED", "LIFECYCLE_OBSERVED", "LIFECYCLE_ACTIVE",
    "LIFECYCLE_SWAPPED", "LIFECYCLE_RETAINED", "LIFECYCLE_CLOSED",
    "LIFECYCLE_STATES",
    "INTENT_FUNNEL_CONFIRMATION", "INTENT_LEAVE_WARNING",
    "INTENT_DESTRUCTIVE_CONFIRMATION", "INTENT_NOTICE",
    "INTENT_PROMPT_UNANSWERABLE",
    "ACTION_ACCEPT", "ACTION_DISMISS",
    "DIALOG_ALERT", "DIALOG_CONFIRM", "DIALOG_PROMPT", "DIALOG_BEFOREUNLOAD",
    "DialogDecision", "resolve_dialog",
    "ADOPT", "RETAIN", "POPUP_CLOSED", "POPUP_BLANK", "POPUP_OUT_OF_SCOPE",
    "POPUP_SUPERSEDED", "PopupDecision", "resolve_popup",
    "PageEntry", "PageRegistry",
    "EVENT_POPUP", "EVENT_DIALOG", "EVENT_DOWNLOAD", "EVENT_PAGE_CLOSED",
    "popup_record", "dialog_record", "download_record", "page_closed_record",
    "safe_artifact_name", "content_type_for", "CONTENT_TYPE_BY_SUFFIX",
    "is_blank_url",
]
