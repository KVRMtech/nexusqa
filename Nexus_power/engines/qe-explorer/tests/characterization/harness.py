"""Deterministic crawl harness — the instrument M0.3 is measured with.

WHY A HARNESS AND NOT A LIVE CRAWL.  The milestone demands byte-identical
output before and after every extraction.  A live crawl cannot deliver that
and never could: wall-clock timestamps, network jitter, LLM oracle replies and
the target application's own state all move between runs.  Diffing two live
crawls measures the weather.

So the crawl is made deterministic at its four real sources of entropy, and
NOTHING else is touched:

  1. TIME      — ``emit.MonotonicClock`` is replaced by a counter that returns
                 +1ms per read.  This is deliberately the most sensitive choice
                 available: the manifest timestamps then encode the exact
                 SEQUENCE of clock reads, so an extraction that reorders work,
                 reads the clock an extra time, or drops a read shows up as a
                 diff.  A pure move never changes that sequence.
  1b. THE DATE — ``date.today()`` is frozen.  Value synthesis derives a
                 date-of-birth from an age band and defaults date/month/week
                 fields to today, so an unfrozen calendar rolls the manifest
                 over at midnight.  This was found the hard way: goldens minted
                 on one day failed the next with a one-day DOB shift and nothing
                 else.  A gate that goes red on its own once a day is a gate
                 people learn to ignore, which is worse than no gate.
  2. BROWSER   — a scripted :class:`ScriptedBrowser` implementing the real
                 :class:`app.browser.BrowserPort`.  Same port the production
                 adapter implements, so the crawler cannot tell the difference.
  3. ORACLES   — fixed-reply stubs.  An LLM is not a function; a golden that
                 called one would not be a golden.
  4. FILESYSTEM— the crawl runs in a temp dir; the manifest is read back and
                 the temp path is scrubbed before comparison.

What is NOT stubbed: the frontier, the budget, the guard, the fingerprinter,
the forms engine, the inventory, the flow ledger, the coverage builder and the
whole crawler state machine.  Those are the subject under test and they run
for real.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app import emit
from app import field_values
from app import forms
from app import identity_pack
from app.auth import Credentials
from app.browser import BrowserPort, NavResult, RawObservation
from app.config import Settings
from app.crawler import Budget, Crawler, GuardContext
from app.guard import Attestation, load_refuse_pack

_REFUSE_PACK = load_refuse_pack(Settings().refuse_pack_path)

#: A minimal valid 1x1 PNG.  Deterministic bytes -> deterministic screenshot
#: files, so the manifest's screenshot refs are stable.
PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGNgAAIAAAUAAen63NgAAAAASUVORK5CYII="
)

GOLDEN_DIR = Path(__file__).parent / "goldens"


# ─── 1. Deterministic time ───────────────────────────────────────────────────


#: The frozen calendar day every golden is captured on.  Arbitrary but fixed;
#: changing it re-mints every golden, so it must not be touched casually.
FROZEN_TODAY = _date(2026, 8, 15)


class FrozenDate(_date):
    """``date`` with a pinned ``today()``.

    A subclass rather than a stub so ``timedelta`` arithmetic, ``isoformat``
    and ``isocalendar`` all behave exactly as the real class does — only the
    reading of the wall calendar is pinned.
    """

    @classmethod
    def today(cls) -> "_date":
        return FROZEN_TODAY


#: Modules that read the wall calendar during value synthesis.  Each does
#: ``from datetime import date``, so the name must be patched per module.
_DATE_CONSUMERS = (field_values, forms, identity_pack)


class TickClock:
    """Duck-types :class:`app.emit.MonotonicClock`; +1ms per read.

    Seeded from ``offset_ms`` exactly as the real clock is, so the resume path
    (which seeds the offset from the manifest prefix) behaves identically.
    """

    def __init__(self, offset_ms: int = 0) -> None:
        self._t = max(0, int(offset_ms))

    def now_ms(self) -> int:
        self._t += 1
        return self._t


# ─── 2. Scripted browser ─────────────────────────────────────────────────────


@dataclass
class ScriptedPage:
    """One page the fake serves.

    ``transitions`` maps a control's accessible name to the page key it leads
    to.  Because a page key is distinct from a URL, the same fixture can model
    BOTH a real navigation (next page has a different ``url``) and an SPA
    wizard step (next page keeps the same ``url`` but shows new controls) —
    the two traversal shapes the walker treats very differently.
    """

    url: str
    title: str = ""
    controls: list[dict[str, Any]] = field(default_factory=list)
    transitions: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    displayed_values: list[dict[str, Any]] = field(default_factory=list)
    network: list[dict[str, Any]] = field(default_factory=list)
    dialogs: list[str] = field(default_factory=list)
    #: A4.3 — the application's own DECLARED status regions (role=status /
    #: aria-live=polite) visible on this page. The strongest same-page
    #: confirmation signal there is: the app is telling us, not us inferring.
    statuses: list[str] = field(default_factory=list)
    #: Plain visible text blocks. Modelled because most applications render a
    #: success banner as an undecorated div with no ARIA role at all — including
    #: the one this milestone is proven on — so a fixture that only offered
    #: `statuses` would prove a capability real pages cannot exercise.
    texts: list[str] = field(default_factory=list)
    #: M1.3 — the requests clicking a control ACTUALLY fires, classified by the
    #: REAL guard.  ``{control_name: [{"method": "POST", "url": "..."}]}``.
    #: Empty (the default) means the fake behaves exactly as it always has, so
    #: every existing fixture and golden is untouched.
    emits: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: A server-side precondition for LEAVING this page: the transition fires
    #: only once the named token has been persisted by an ALLOWED request.  This
    #: is what makes the fixture a real proof rather than a mime — a blocked
    #: Save Draft leaves the server without the draft, and the wizard genuinely
    #: cannot advance, which is precisely the production behaviour M1.3 exists
    #: to unblock.
    requires_persisted: str = ""
    #: ``{control_name: token}`` — what an ALLOWED request from this control
    #: persists on the server.
    persists: dict[str, str] = field(default_factory=dict)


def control(role: str, name: str, **over: Any) -> dict[str, Any]:
    """A raw inventory control, matching the real inventory's field set."""
    base = {
        "role": role, "name": name, "name_source": "content", "best_effort": False,
        "kind": over.pop("kind", role), "tag": over.pop("tag", "a" if role == "link" else role),
        "input_type": "", "options": [], "required": False, "disabled": False,
        "frame_selector": "", "testid": "", "css_hint": "", "value_committed": "",
        "landmark": {"role": "", "name": ""},
    }
    base.update(over)
    return base


class ScriptedBrowser(BrowserPort):
    """A fully scripted :class:`BrowserPort` over a page graph.

    Every method the port declares is implemented; none of them raise.  An
    unknown URL is served as a real browser would serve a 404-ish dead end (an
    empty page, ``NavResult.ok=False``) rather than by throwing, because the
    crawler's contract with the port is that failure arrives as an honest
    observation.
    """

    def __init__(self, pages: Mapping[str, ScriptedPage], start: str) -> None:
        self._pages = dict(pages)
        self._key = start
        self._by_url: dict[str, str] = {}
        for key, page in self._pages.items():
            self._by_url.setdefault(page.url, key)
        self.uploads: list[tuple[str, list[str]]] = []
        self.materialize_calls = 0
        self.network_drains = 0
        self._drained: set[str] = set()
        # M1.3 network simulation.  Unbound (the default for every pre-existing
        # fixture) the whole mechanism is inert.
        self._guard: Any = None
        self._now: Any = None
        self.persisted: set[str] = set()
        self.allowed_requests: list[dict[str, Any]] = []
        self.blocked_requests: list[dict[str, Any]] = []

    def bind_crawler(self, crawler: Any) -> None:
        """Wire the fake app's network to the crawl's REAL guard + clock.

        Without this the fake cannot prove anything about the guard; with it,
        every request the scripted app makes is classified by exactly the object
        the Playwright route handler consults in production, on exactly the
        clock the guard's windows are measured against."""
        self._guard = crawler.guard
        self._now = crawler.now_ms

    def _fire(self, control_name: str) -> None:
        """Emit this control's requests through the guard, and persist what it
        allowed.  A BLOCKED request changes no server state — which is the
        whole point of the guard, and the reason a blocked Save Draft leaves the
        wizard genuinely stuck."""
        page = self._page()
        requests = page.emits.get(str(control_name or "")) or ()
        if not requests or self._guard is None:
            return
        for req in requests:
            method = str(req.get("method") or "GET").upper()
            url = str(req.get("url") or "")
            decision = self._guard.decide(method, url, now_ms=int(self._now()))
            record = {"method": method, "url": url, "allow": bool(decision.allow),
                      "rule_id": decision.rule_id}
            if decision.allow:
                self.allowed_requests.append(record)
                token = page.persists.get(str(control_name or ""))
                if token:
                    self.persisted.add(token)
            else:
                self.blocked_requests.append(record)

    # -- internals ------------------------------------------------------------

    def _page(self) -> ScriptedPage:
        return self._pages.get(self._key) or ScriptedPage(url=self._key)

    def _advance(self, name: Any) -> Optional[str]:
        page = self._page()
        dest = page.transitions.get(str(name or ""))
        if dest and dest in self._pages:
            # THE SERVER'S OWN GATE.  A step whose state was never persisted
            # does not render the next step, exactly as a real wizard does not.
            required = str(page.requires_persisted or "")
            if required and required not in self.persisted:
                return None
            self._key = dest
            return self._pages[dest].url
        return None

    # -- navigation / observation --------------------------------------------

    async def goto(self, url: str) -> NavResult:
        key = self._by_url.get(url)
        if key is None:
            # Unknown destination: the fake still "lands" there so the crawler's
            # off-target handling runs, but reports the navigation as failed.
            self._key = url
            return NavResult(url=url, ok=False)
        self._key = key
        return NavResult(url=self._pages[key].url, ok=True)

    async def current_url(self) -> str:
        return self._page().url

    async def title(self) -> str:
        return self._page().title

    async def collect_controls(self) -> list[dict[str, Any]]:
        return [dict(c) for c in self._page().controls]

    async def collect_displayed_values(self) -> list[dict[str, Any]]:
        return [dict(v) for v in self._page().displayed_values]

    async def dialog_flags(self) -> list[str]:
        return list(self._page().dialogs)

    async def error_texts(self) -> list[str]:
        return list(self._page().errors)

    async def status_texts(self) -> list[str]:
        return list(self._page().statuses)

    async def visible_texts(self) -> list[str]:
        return list(self._page().texts)

    async def screenshot_png(self) -> bytes:
        return PNG_1x1

    async def materialize(self) -> None:
        self.materialize_calls += 1

    async def drain_network(self) -> list[dict[str, Any]]:
        self.network_drains += 1
        if self._key in self._drained:
            return []
        self._drained.add(self._key)
        return [dict(n) for n in self._page().network]

    async def storage_state(self) -> dict[str, Any]:
        return {"cookies": [], "origins": []}

    # -- interaction ----------------------------------------------------------

    async def click(self, control: Mapping[str, Any]) -> RawObservation:
        before = self._page().url
        # ORDER MATTERS: the request fires FIRST (the browser sends it as the
        # click is handled), and only its outcome decides whether the server
        # will serve the next step.
        self._fire(control.get("name"))
        after = self._advance(control.get("name"))
        return RawObservation(url_before=before, url_after=after or before)

    async def click_at(self, x: int, y: int) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url)

    async def hover(self, control: Mapping[str, Any]) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url)

    async def drag(self, path: Sequence[Sequence[int]]) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url)

    async def draw_stroke(self, points: Sequence[Sequence[int]]) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url)

    async def press_keys(self, keys: Sequence[str]) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url)

    async def scroll_until(self, control: Mapping[str, Any], *a: Any, **k: Any) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url)

    async def set_input_files(self, control: Mapping[str, Any],
                              paths: Sequence[str]) -> RawObservation:
        self.uploads.append((str(control.get("name") or ""), list(paths)))
        name = paths[0].replace("\\", "/").rsplit("/", 1)[-1] if paths else ""
        url = self._page().url
        return RawObservation(url_before=url, url_after=url, committed_value=name)

    async def fill(self, control: Mapping[str, Any], value: str) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url, committed_value=value)

    async def select_option(self, control: Mapping[str, Any], value: str) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url, committed_value=value)

    async def set_checked(self, control: Mapping[str, Any], checked: bool) -> RawObservation:
        url = self._page().url
        return RawObservation(url_before=url, url_after=url,
                              committed_value="true" if checked else "false")


# ─── 3. Oracle stubs ─────────────────────────────────────────────────────────


def advance_oracle_stub(reply: Optional[dict[str, Any]] = None):
    """A tier-3 advance oracle with a FIXED reply and a call log."""
    calls: list[tuple[int, str, str]] = []
    fixed = dict(reply or {"index": None, "status": "none", "signature": ""})

    async def oracle(controls: Sequence[dict[str, Any]], page_title: str,
                     page_url: str) -> dict[str, Any]:
        calls.append((len(controls), page_title, page_url))
        return dict(fixed)

    oracle.calls = calls  # type: ignore[attr-defined]
    return oracle


def vision_oracle_stub(reply: Optional[dict[str, Any]] = None):
    """A U2 vision oracle with a FIXED reply and a call log."""
    calls: list[str] = []
    fixed = dict(reply or {"controls": [], "displayed_values": []})

    async def perceive(screenshot_b64: str, page_context: dict[str, Any]) -> dict[str, Any]:
        calls.append(str(page_context.get("url") or ""))
        return dict(fixed)

    perceive.calls = calls  # type: ignore[attr-defined]
    return perceive


async def no_sleep(_seconds: float) -> None:
    return None


# ─── 4. Running a fixture and capturing the golden ───────────────────────────


def disposable_attestation(expires_at_ms: int = 4_102_444_800_000) -> Attestation:
    """A disposable-env attestation (default expiry: year 2100, so a golden
    captured today does not rot into a different code path tomorrow)."""
    return Attestation(attested_by="characterization",
                       env_kind="disposable",
                       reset_procedure="rebuild",
                       expires_at_ms=expires_at_ms)


@dataclass
class Fixture:
    """One reproducible crawl scenario."""

    name: str
    pages: dict[str, ScriptedPage]
    start: str
    target_url: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    advance_reply: Optional[dict[str, Any]] = None
    vision_reply: Optional[dict[str, Any]] = None
    #: A ``ScriptedBrowser`` subclass for fixtures whose application must
    #: REMEMBER what was typed (a schema that accepts the second value after
    #: refusing the first — B2's shape).  ``None`` — every pre-existing
    #: fixture — constructs the plain fake exactly as before.
    port_factory: Optional[Any] = None


_TMP_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\"]*?[\\/]qec_char_[^\"\\/]*")


def _scrub(text: str, work_dir: str) -> str:
    """Remove the one genuinely non-reproducible token: the temp work dir."""
    for variant in {work_dir, work_dir.replace("\\", "/"), work_dir.replace("\\", "\\\\")}:
        text = text.replace(variant, "<WORK_DIR>")
    return _TMP_PATH_RE.sub("<WORK_DIR>", text)


def run_fixture(fixture: Fixture, work_dir: Path,
                monkeypatch: Any) -> tuple[str, dict[str, Any]]:
    """Run one fixture end-to-end and return (manifest_text, summary_dict).

    ``manifest_text`` is the crawl manifest verbatim — canonical JSON lines,
    sorted keys, one record per line — with the temp path scrubbed.  That text
    IS the golden: it carries every state, every action, every edge, every
    guard event and both meta records, in emission order.
    """
    monkeypatch.setattr(emit, "MonotonicClock", TickClock)
    for module in _DATE_CONSUMERS:
        if hasattr(module, "date"):
            monkeypatch.setattr(module, "date", FrozenDate)

    port = (fixture.port_factory or ScriptedBrowser)(fixture.pages, fixture.start)
    # COPY, never pop the fixture's own dict. ``Fixture.kwargs`` is a MODULE-LEVEL
    # mapping shared by every consumer, so popping from it permanently stripped
    # the fixture of its guard context after the first run - F3's disposable
    # attestation vanished, and any later test reusing F3 silently got a crawl
    # that could no longer cross a boundary. Harmless while this harness had one
    # caller; a landmine the moment it had two.
    fixture_kwargs = dict(fixture.kwargs)
    guard_ctx = fixture_kwargs.pop("guard_context", None) or GuardContext(
        refuse_pack=_REFUSE_PACK)

    kwargs: dict[str, Any] = dict(
        crawl_id="char-crawl", tenant_id="char-tenant",
        target_url=fixture.target_url, work_dir=str(work_dir),
        refuse_pack=_REFUSE_PACK, budget=Budget(rate_per_s=0),
        explorer_version="char/1.0", guard_version="char",
        refuse_pack_version=_REFUSE_PACK.version, config_fingerprint="char-fp",
        guard_context=guard_ctx, sleep=no_sleep,
        advance_oracle=advance_oracle_stub(fixture.advance_reply),
        vision_oracle=vision_oracle_stub(fixture.vision_reply),
    )
    kwargs.update(fixture_kwargs)

    crawler = Crawler(port, **kwargs)
    # Wire the fake app's network to the crawl's real guard.  Inert for every
    # fixture that declares no ``emits``.
    if hasattr(port, "bind_crawler"):
        port.bind_crawler(crawler)
    summary = asyncio.run(crawler.run())

    manifest = emit.manifest_path(str(work_dir), "char-crawl")
    text = manifest.read_text(encoding="utf-8") if manifest.exists() else ""

    digest = {
        "stop_reason": summary.stop_reason,
        "states": summary.states,
        "actions": summary.actions,
        "screenshots": summary.screenshots,
        "guard_blocks": summary.guard_blocks,
        "detail": summary.detail,
        "coverage": summary.coverage,
    }
    blob = _scrub(text, str(work_dir))
    digest_text = json.dumps(digest, sort_keys=True, indent=2, ensure_ascii=True)
    return blob + "\n===SUMMARY===\n" + _scrub(digest_text, str(work_dir)) + "\n", digest


def golden_path(name: str) -> Path:
    return GOLDEN_DIR / f"{name}.golden"
