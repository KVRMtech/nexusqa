"""Browser-side control-inventory walker (injected JS as a Python constant).

This module holds ONE thing: :data:`INVENTORY_JS`, an ES5-safe JavaScript
expression that :mod:`app.crawler` hands to Playwright ``page.evaluate`` /
``frame.evaluate``.  It runs INSIDE the crawled page and returns a JSON-
serialisable ``Array<RawControl>`` — the raw material :func:`app.inventory.
build_inventory` refines into the compiler's locator vocabulary.

Design references (verified against the local repo 2026-07-08):

  * accessible-name subset & control-kind vocabulary — design §3.2 and the
    compiler ladder ``platform/api/app/services/script_factory/compiler.py``
    (``_ladder`` :297-331, ``_ANCHOR_ROLE`` :216-225, ``_refine_kind``
    :174-208).  Only the USER-FACING accessible name is bindable; ``testid``
    and ``css_hint`` have NO compiler rung, so they are captured as pure
    diagnostics.
  * iframe selector recipe mirrors the heal-capture afterEach in the frozen
    compiler (compiler.py:1005-1011) so a ``frame_selector`` we emit resolves
    the SAME way ``page.frameLocator(...)`` resolves it.

Accessible-name ladder (design order — label[for] FIRST, deliberately):

    1. ``<label for=id>``      → name_source "label-for"
    2. ``aria-labelledby``     → "aria-labelledby"
    3. ``aria-label``          → "aria-label"
    4. wrapping ``<label>``    → "wrapping-label"
    5. name-from-content text  → "content"      (buttons / links / menuitems)
    6. ``title``               → "title"        (best_effort)
    7. ``placeholder``         → "placeholder"   (best_effort)

``best_effort`` is set on rungs 6-7 — a placeholder/title is NOT a reliable
accessible name and the refiner surfaces that as an a11y weakness rather than
silently trusting it.

RawControl shape (one object per visible interactive element):

    role            implicit-or-explicit ARIA role (lower-case)
    name            accessible name (may be "" — honest, never invented)
    name_source     which ladder rung produced ``name``
    best_effort     true when ``name`` came from title/placeholder
    kind            NAIVE kind hint (the Python refiner is authoritative)
    tag             lower-case tagName
    input_type      ``<input>`` type (lower-case) or ""
    autocomplete    the W3C autocomplete token(s) the app declared, lower-cased.
                    THE STRONGEST semantic signal there is — see the classifier
                    contract below.
    inputmode       the inputmode keyword, lower-cased. A weaker declaration of
                    the same kind.
    placeholder     the placeholder attribute, VERBATIM (human-authored text)
    id              the id attribute, VERBATIM (case-sensitive; it has to
                    round-trip through getElementById and CSS.escape)
    options         visible option labels for a native ``<select>``, up to
                    :data:`MAX_OPTIONS`
    group_key       the mutually-exclusive CHOICE GROUP this control answers
                    ("name:<form>:<attr>" for native radios, "grp:<container>"
                    for ARIA radiogroup/fieldset sets, "" when ungrouped).
                    Structure, never a value — it says WHICH QUESTION, not what
                    anyone answered.
    question_key    the DECLARED question container this control sits in
                    ("q:<container>"), "" when the page declared none.  Wider
                    than ``group_key``: a bare-<button> Yes/No pair inside a
                    <fieldset> is one question too, and grouping by radio
                    semantics alone could never see it.
    question_label  the application's OWN wording for that question, from a
                    declared accessible-name rung only (aria-labelledby →
                    aria-label → <legend> → heading inside the container).
                    "" when nothing was declared — never inferred from layout.
    question_label_source
                    which rung produced it, so a reader can weigh it
    required        required || aria-required
    disabled        disabled || aria-disabled
    frame_selector  ""  for the main frame, else the owning iframe selector
    testid          data-testid|data-test|data-cy|data-qa (diagnostic)
    css_hint        short tag#id.class selector (diagnostic)
    value_committed committed value ("" for password / non-value controls)
    href            resolved link destination for <a> (diagnostic; drives the
                    crawler's href-follow traversal), "" for non-anchors
    haspopup        aria-haspopup value (menu/listbox/true/…) — marks a hover/menu
                    trigger the crawler hovers to reveal a fly-out, "" otherwise
    expanded        aria-expanded value (true/false) — marks a CLICK-to-open
                    dropdown/disclosure toggle (a Bootstrap ``dropdown-toggle`` etc.
                    whose menu items are hidden until it is clicked), "" otherwise
    landmark        {role, name} of the nearest landmark ancestor (anchor seed)
    filter_scope    "thead" when the control sits in a data table's FILTER
                    row, "tbody"/"tfoot" for the data itself, "" otherwise.
                    Structural: a list filter is not a business field.

THE CAPTURE COMPLETENESS CONTRACT (M0.x)
========================================

The goal is not to capture every DOM property.  It is to capture everything the
downstream classifier and crawler contractually depend on — because a field this
walker omits is not a field a later layer can recover.  Each of these is here
because a NAMED consumer reads it:

  *identity*        ``id``, ``role``, ``tag``, ``name`` — the control, and the
                    thing the compiler binds by.
  *classification*  ``autocomplete``, ``inputmode``, ``placeholder``.
                    :func:`app.field_semantics.classify` ranks ``autocomplete``
                    FIRST, at confidence 0.98, because it is the application's
                    own W3C-standard statement of what the field is for — right
                    far more often than any reading of a label.  ``placeholder``
                    and ``id`` are :func:`app.field_signature.compute`'s token
                    fallbacks for a control with NO accessible name.  All four
                    were read by those consumers and emitted by nothing, so rung
                    1 was unreachable code on every crawled control.
  *accessibility*   ``name_source``, ``best_effort``, and the aria attributes the
                    name ladder consumes.  A name is computed the way W3C accname
                    computes it (RENDERED text, via ``accText``) on EVERY rung,
                    including the ``aria-labelledby`` id-reference rung — a name
                    computed any other way is a name no locator can bind.
  *structure*       ``frame_selector`` (escaped, so it resolves the same way
                    ``page.frameLocator()`` resolves it) and shadow-root context.
  *enumerations*    ``options`` plus ``options_total``, bounded by ONE ceiling
                    (:data:`MAX_OPTIONS`) shared with the refiner and the
                    catalogue.  ``options_total`` is what keeps a clipped read
                    from being catalogued as a complete answer set.
  *declared rules*  ``required``, ``disabled``, ``min``/``max``/``step``,
                    ``pattern``, ``minlength``/``maxlength`` — what the app said
                    about its own field, which is what a boundary or negative
                    scenario is derived from.

The walker recurses OPEN shadow roots (same frame, no selector change — the
compiler's getByRole/getByLabel pierce open shadow DOM automatically), resolving
names against the shadow root that OWNS the element, since an id reference does
not cross a shadow boundary; and SAME-ORIGIN iframes (cross-origin
``contentDocument`` access throws and is skipped honestly).

It is executed for real against fixture pages in ``tests/browser`` — both in
jsdom and in Chromium through the production ``PlaywrightBrowserPort`` — and the
capture contract above is enforced there by ``test_capture_contract.py``.
"""
from __future__ import annotations

#: Stamped into the crawl manifest so a manifest can be traced to the exact
#: injected-JS generation that produced its controls.
INVENTORY_JS_VERSION = "inv-js-v12"

#: THE OPTION CEILING. One number, for the whole pipeline.
#:
#: It lives in Python and is interpolated into the JavaScript below so that the
#: walker, the refiner (:data:`app.inventory.MAX_OPTIONS`) and the catalogue
#: (``MAX_CATALOG_OPTIONS`` in qe-central) cannot drift apart. They did: the
#: walker captured 300, the refiner kept 60 and the catalogue's update path kept
#: 48, so a 250-country question arrived downstream as its first 48 answers.
#:
#: Sized for COMPLETENESS of the enumerations a business form actually asks:
#: 50 US states, ~250 countries, a 100-year date-of-birth range. Still bounded —
#: an unbounded read would let one pathological ``<select>`` dominate the
#: manifest — and when it does clip, ``options_total`` reports the true size so a
#: prefix is never presented as the whole answer set.
MAX_OPTIONS = 300

#: M3.2 / T-FR-02 — THE CAPTURE INIT SCRIPT, installed at BROWSER-CONTEXT
#: creation (``context.add_init_script``) and therefore evaluated in every page
#: and every frame BEFORE a single line of application script runs.
#:
#: WHY IT HAS TO BE HERE AND NOT ANYWHERE ELSE.  A closed shadow root is handed
#: to its component and to nobody else: ``attachShadow({mode:"closed"})`` returns
#: the root, ``el.shadowRoot`` stays ``null`` forever, and there is no API that
#: recovers it afterwards.  A retrofit — patching, querying or re-attaching once
#: the component exists — cannot work, because the root it would need was
#: created and captured before we arrived.  The only moment at which the root is
#: observable is the moment it is created, so the observation has to be in place
#: before the component's constructor runs.  That moment is context creation.
#:
#: WHAT THIS IS AND IS NOT.  A closed shadow root is an ENCAPSULATION
#: convention, not a security boundary — the HTML spec says so in as many words,
#: and the DOM it hides is same-origin, same-process content the page already
#: rendered to the user.  Wrapping ``attachShadow`` inside our own automation
#: context therefore crosses no browser security boundary: nothing here reaches
#: another origin, another process or another context.  Contrast the iframe half
#: of M3.2, which deliberately refuses to inject anything across an origin
#: boundary and uses Playwright's own frame APIs instead.
#:
#: WHAT IT PROMISES THE PAGE.  The application must not be able to tell.  The
#: native method is called with the caller's own arguments and its return value
#: is returned unchanged; ``el.shadowRoot`` still reads ``null`` for a closed
#: root; the replacement is non-enumerable, carries the native ``name`` and
#: ``length`` and forwards ``toString`` to the native implementation, so a
#: framework that fingerprints ``attachShadow`` sees what it expects.  The roots
#: are held in a ``WeakMap`` — no leak, and no property the page can enumerate —
#: reachable only through a non-enumerable ``window.__nxCaptureHooks``.
#:
#: DECLARATIVE shadow DOM (``<template shadowrootmode="closed">``) does NOT go
#: through ``attachShadow`` and is therefore NOT observed by this hook.  That is
#: stated rather than papered over: such a surface stays a named opaque row.
CAPTURE_HOOKS_JS = r"""
(function () {
  try {
    if (window.__nxCaptureHooks) return;
    var native = Element.prototype.attachShadow;
    if (typeof native !== "function") return;
    var roots = new WeakMap();
    var patched = function attachShadow(init) {
      var root = native.apply(this, arguments);
      try {
        if (init && init.mode === "closed") roots.set(this, root);
      } catch (e) { /* a host that is not weak-mappable is simply not observed */ }
      return root;
    };
    try {
      Object.defineProperty(patched, "name", { value: "attachShadow", configurable: true });
      Object.defineProperty(patched, "length", { value: 1, configurable: true });
      patched.toString = function () { return native.toString(); };
    } catch (e) {}
    Object.defineProperty(Element.prototype, "attachShadow", {
      value: patched, writable: true, configurable: true, enumerable: false });
    var hooks = {
      installed: true,
      closedRoot: function (el) {
        try { return roots.get(el) || null; } catch (e) { return null; }
      }
    };
    try {
      Object.defineProperty(window, "__nxCaptureHooks", {
        value: hooks, writable: false, configurable: true, enumerable: false });
    } catch (e) { window.__nxCaptureHooks = hooks; }
  } catch (e) { /* a page that refuses the hook is simply as blind as before */ }
})();
"""

#: Bumped when the init script changes, so a manifest can be traced back to the
#: capture hooks that were in force when it was produced.
CAPTURE_HOOKS_JS_VERSION = "hooks-js-v1"


#: Shared walker block: how capture asks an element for its shadow root.
#:
#: Injected into BOTH injected snippets so there is exactly one answer to "what
#: is inside this custom element".  Absent the init script both helpers return
#: null and every consumer is exactly as blind as it was before — the hook can
#: never manufacture a capture, only reveal one.
_SHADOW_HOOK_JS = r"""
  function closedRootOf(el) {
    try {
      var h = window.__nxCaptureHooks;
      return (h && typeof h.closedRoot === "function") ? (h.closedRoot(el) || null) : null;
    } catch (e) { return null; }
  }

  function shadowRootOf(el) {
    try { if (el && el.shadowRoot) return el.shadowRoot; } catch (e) {}
    return closedRootOf(el);
  }
"""


#: Shared walker block: the iframe-selector recipe (mirrors compiler.py:1005-1011).
#:
#: ONE recipe, injected into both snippets, because the snippet that DETECTS a
#: frame and the snippet that CAPTURES inside one must name it identically — a
#: second spelling is a frame the port enters and then cannot attribute.
#:
#: Depends on ``_SHADOW_HOOK_JS`` (a frame nested in a shadow root belongs to
#: the same document's frame set), so that block is injected first.
_FRAME_SELECTOR_JS = r"""
  // An IDENTIFIER in selector position — an id following '#'. CSS.escape is the
  // standard answer, and the label[for] lookup elsewhere in this walker already
  // uses it; this recipe simply never did. Unescaped, id="pay.frame" yields
  // `iframe#pay.frame`, which is VALID CSS that means something else entirely
  // ("id=pay AND class=frame") — so it fails silently, matching nothing, while
  // the manifest records every control in that frame as captured.
  function cssIdent(s) {
    try { return CSS.escape(s); }
    catch (e) { return ("" + s).replace(/([^a-zA-Z0-9_-])/g, "\\$1"); }
  }

  // A STRING in attribute-value position. Inside a quoted CSS string only the
  // quote itself and the backslash need escaping — spaces, brackets,
  // apostrophes and punctuation are all already legal there, so a title like
  // `customer's [account]` needs no mangling. A literal newline is not legal and
  // takes the CSS \A escape. Unescaped, name='quote"frame' yielded
  // `iframe[name="quote"frame"]`, which is not parseable CSS at all.
  function cssStr(s) {
    return ("" + (s == null ? "" : s))
      .replace(/\\/g, "\\\\")
      .replace(/"/g, "\\\"")
      .replace(/\n/g, "\\A ");
  }

  function fsAttr(el, name) {
    try { var v = el.getAttribute(name); return v == null ? "" : v; }
    catch (e) { return ""; }
  }

  function fsTag(el) {
    try { return ("" + (el.tagName || "")).toLowerCase(); } catch (e) { return ""; }
  }

  // The ROOT NODE an element belongs to: its document, or the shadow root that
  // encapsulates it.  Everything below is scoped to this rather than to the
  // page, which is the whole of T-FR-03.
  function fsRootOf(el) {
    try { return el.getRootNode ? el.getRootNode() : el.ownerDocument; }
    catch (e) { try { return el.ownerDocument; } catch (e2) { return null; } }
  }

  function fsHostOf(root) {
    try { return (root && root.nodeType === 11 && root.host) ? root.host : null; }
    catch (e) { return null; }
  }

  // EVERY iframe reachable from ONE root, IN THE ORDER PLAYWRIGHT RESOLVES THEM.
  //
  // M3.2 / T-FR-03.  The positional rung emits `iframe >> nth=N`, and N only
  // means anything if it counts the same frames, in the same order, as the
  // engine that will resolve it.  Two things were wrong.  The ordinal was the
  // loop index inside whichever subtree the walker was standing in, so a frame
  // nested in a shadow root was numbered among its shadow siblings and the
  // selector bound to a completely different frame.  And the order has to be
  // BREADTH-FIRST — this root's own tree first, then the shadow roots hanging
  // off it — because that is what Playwright's shadow-piercing CSS engine does,
  // as fixture 23 demonstrates by handing the emitted selectors back to a real
  // browser.  A depth-first, tree-order list looks more natural and is wrong.
  //
  // Memoised per root: the walker asks once per frame it descends.
  var FRAME_ROOTS = [], FRAME_LISTS = [];
  function framesOf(rootNode) {
    var at = FRAME_ROOTS.indexOf(rootNode);
    if (at >= 0) return FRAME_LISTS[at];
    var out = [], queue = [rootNode];
    function scanRoot(r) {
      var list;
      try { list = r.querySelectorAll("*"); } catch (e) { return; }
      for (var i = 0; i < list.length; i++) {
        var el = list[i];
        if (fsTag(el) === "iframe") out.push(el);
        var sr = shadowRootOf(el);
        if (sr) queue.push(sr);
      }
    }
    while (queue.length) { scanRoot(queue.shift()); }
    FRAME_ROOTS.push(rootNode); FRAME_LISTS.push(out);
    return out;
  }

  // The chained prefix that ADDRESSES an element's root: "" for a document, and
  // `<hostSelector> >> ` for a shadow root, recursively.  Playwright scopes the
  // right-hand side of `>>` to the left-hand element's subtree and pierces open
  // shadow roots on the way in, so this is a supported, deterministic address
  // for a frame the document-wide ordinal cannot safely name.
  function frameScopeOf(el) {
    var host = fsHostOf(fsRootOf(el));
    if (!host) return "";
    return frameScopeOf(host) + hostSelectorFor(host) + " >> ";
  }

  function hostSelectorFor(host) {
    var tag = fsTag(host) || "*";
    try { if (host.id) return tag + "#" + cssIdent(host.id); } catch (e) {}
    var siblings = [];
    try { siblings = fsRootOf(host).querySelectorAll(tag); } catch (e) { siblings = []; }
    if (siblings.length <= 1) return tag;
    var at = Array.prototype.indexOf.call(siblings, host);
    return at >= 0 ? (tag + " >> nth=" + at) : tag;
  }

  // Which of this root's frames ALSO carry `name`=`value`.
  function framePeersBy(peers, name, value) {
    var out = [];
    for (var i = 0; i < peers.length; i++) {
      var v = (name === "id") ? (peers[i].id || "") : fsAttr(peers[i], name);
      if (v === value) out.push(peers[i]);
    }
    return out;
  }

  // DETERMINISM, not merely escaping (M3.2 / T-FR-03).
  //
  // Escaping makes a selector PARSE. It does not make it identify ONE frame. A
  // page with two `<iframe title="Payment">` embeds emitted the same selector
  // twice; `frameLocator` takes the first match and says nothing, so half the
  // captured controls were recorded against a frame they are not in — the worst
  // failure available, because it reads as coverage. When a rung's value is not
  // unique among the frames its own scope can see, the selector is disambiguated
  // by the frame's ordinal AMONG ITS OWN MATCHES, which is exactly what `>> nth=`
  // means to Playwright.
  function frameRung(scope, sel, peers, name, value, iframeEl) {
    var matches = framePeersBy(peers, name, value);
    if (matches.length <= 1) return scope + sel;
    var at = matches.indexOf(iframeEl);
    return scope + sel + " >> nth=" + (at < 0 ? 0 : at);
  }

  function frameSelectorFor(iframeEl, index) {
    var scope = "", peers = [];
    try {
      scope = frameScopeOf(iframeEl);
      peers = framesOf(fsRootOf(iframeEl));
    } catch (e) { scope = ""; peers = []; }
    var gi = peers.indexOf(iframeEl);
    if (gi < 0) gi = index;
    try {
      var id = iframeEl.id;
      if (id) return frameRung(scope, 'iframe#' + cssIdent(id), peers, "id", id, iframeEl);
      var nm = fsAttr(iframeEl, "name");
      if (nm) return frameRung(scope, 'iframe[name="' + cssStr(nm) + '"]', peers, "name", nm, iframeEl);
      var title = fsAttr(iframeEl, "title");
      if (title) return frameRung(scope, 'iframe[title="' + cssStr(title) + '"]', peers, "title", title, iframeEl);
      var src = fsAttr(iframeEl, "src");
      if (src) return frameRung(scope, 'iframe[src="' + cssStr(src) + '"]', peers, "src", src, iframeEl);
    } catch (e) {}
    return scope + "iframe >> nth=" + gi;
  }
"""


INVENTORY_JS = (r"""
(() => {
  "use strict";

  // Interactive candidates — a superset; the refiner drops what it cannot bind.
  var SELECTOR = [
    "a[href]", "button", "input", "select", "textarea", "summary",
    "[role]", "[contenteditable=\"\"]", "[contenteditable=\"true\"]",
    "[tabindex]"
  ].join(",");

  var MAX_NAME = 500;
  var MAX_OPTION = 200;
  // THE option ceiling — interpolated from app.inventory_js.MAX_OPTIONS so this
  // layer cannot drift from the refiner and the catalogue. See that constant for
  // why the number is what it is.
  var MAX_OPTIONS = __MAX_OPTIONS__;
  var MAX_LANDMARK = 80;
  var MAX_VALUE = 1000;

  // ---- small helpers -------------------------------------------------------

  function norm(s) {
    return (("" + (s == null ? "" : s)).replace(/\s+/g, " ")).trim();
  }
  function lc(s) { return norm(s).toLowerCase(); }
  function clip(s, n) { s = "" + (s == null ? "" : s); return s.length > n ? s.slice(0, n) : s; }

  function attr(el, name) {
    try { return el.getAttribute(name) || ""; } catch (e) { return ""; }
  }

  // Climb out of a node, crossing an OPEN shadow boundary to the host. A node
  // directly under a shadow root has parentElement === null (its parentNode is
  // the ShadowRoot, not an Element), so a plain parentElement walk would stop
  // there and miss a display:none HOST.
  function parentOrHost(node) {
    var p = node.parentElement;
    if (p) return p;
    var r = node.parentNode;
    if (r && r.nodeType === 11 && r.host) return r.host;   // ShadowRoot
    return null;
  }

  // ANCESTOR-AWARE hiding.
  //
  // `display` is not an inherited property, so getComputedStyle(child).display
  // for a control inside a display:none parent returns the CHILD's own display
  // ("inline-block", …), not "none". Checking only the element therefore
  // reported every control inside a collapsed accordion, an inactive wizard
  // step, a closed <details> and a hidden modal as visible — they were
  // catalogued as present controls the crawler would then try to fill or click.
  // Confirmed in real Chromium, not inferred.
  //
  // The signal a browser normally uses is LAYOUT (offsetParent === null /
  // getClientRects().length === 0). Deliberately not used: jsdom has no layout
  // engine and returns zeros for everything, so a layout gate would make the
  // jsdom lane capture NOTHING and silently pass as "no controls found". This
  // walks the STYLE tree instead — which both engines model identically — so
  // the two lanes stay comparable.
  function hiddenByAncestor(el) {
    var view = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    var node = parentOrHost(el);
    var hops = 0;
    while (node && hops < 200) {
      if (node.hasAttribute("hidden")) return true;
      if (lc(attr(node, "aria-hidden")) === "true") return true;
      // A closed <details> renders nothing but its <summary>.
      if (node.tagName === "DETAILS" && !node.hasAttribute("open")) {
        var inSummary = false;
        var p = el;
        while (p && p !== node) {
          if (p.tagName === "SUMMARY") { inSummary = true; break; }
          p = parentOrHost(p);
        }
        if (!inSummary) return true;
      }
      try {
        var st = view.getComputedStyle(node);
        if (st && st.display === "none") return true;
      } catch (e) {}
      node = parentOrHost(node);
      hops++;
    }
    return false;
  }

  function isVisible(el) {
    try {
      if (!el || el.nodeType !== 1) return false;
      if (el.hasAttribute("hidden")) return false;
      if (lc(attr(el, "aria-hidden")) === "true") return false;
      if (el.tagName === "INPUT" && lc(el.type) === "hidden") return false;
      var st = (el.ownerDocument.defaultView || window).getComputedStyle(el);
      if (st) {
        if (st.display === "none" || st.visibility === "hidden") return false;
        if (parseFloat(st.opacity || "1") === 0) return false;
      }
      if (hiddenByAncestor(el)) return false;
      return true;
    } catch (e) { return true; }
  }

  // Implicit ARIA role from tag + type (subset — enough for the compiler).
  function implicitRole(el) {
    var explicit = lc(attr(el, "role"));
    if (explicit) return explicit;
    var tag = lc(el.tagName);
    var type = lc(el.type || "");
    if (tag === "a") return el.hasAttribute("href") ? "link" : "";
    if (tag === "button") return "button";
    if (tag === "select") return el.multiple ? "listbox" : "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "summary") return "button";
    if (tag === "input") {
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      if (type === "range") return "slider";
      if (type === "button" || type === "submit" || type === "reset" || type === "image") return "button";
      if (type === "search") return "searchbox";
      if (type === "number") return "spinbutton";
      return "textbox";
    }
    if (el.isContentEditable) return "textbox";
    return "";
  }

  function nameFromContentRole(role, tag) {
    if (tag === "button" || tag === "a" || tag === "summary") return true;
    return role === "button" || role === "link" || role === "menuitem" ||
           role === "tab" || role === "option" || role === "checkbox" ||
           role === "radio" || role === "switch";
  }

  // The text an id-referenced element contributes to an accessible name.
  //
  // MUST go through accText(), for exactly the reason accText() documents below:
  // W3C accname follows the RENDERED text, so two block children contribute
  // "A B". This read used norm(textContent) and produced "AB" — a name no
  // locator can match — while the sibling label[for] and wrapping-label rungs
  // twelve lines down had already been converted to accText(). aria-labelledby
  // pointing at a title+body pair is the standard markup for a questionnaire
  // question, so the rung that was left behind is the one business forms use
  // most.
  function idText(doc, id) {
    if (!id) return "";
    try {
      return accText(doc.getElementById(id));
    } catch (e) { return ""; }
  }

  // The text an element contributes to an ACCESSIBLE NAME.
  //
  // MUST NOT be textContent. W3C accname — and therefore Playwright's
  // getByRole(name=…), every AT, and the compiler's own binding rung — follows
  // the RENDERED text, so two block children contribute "A B". textContent
  // concatenates them with nothing and yields "AB".
  //
  // Observed live: a product card built as <div>Term Life Insurance</div>
  // <div>Affordable coverage…</div> was named
  // "Term Life InsuranceAffordable coverage…", while Playwright computed
  // "Term Life Insurance Affordable coverage…". get_by_role(name=…) matched
  // ZERO elements, so every fill on that card timed out and was recorded
  // intent_unmet — the control was unusable, and any generated script binding
  // by that name would fail the same way.
  //
  // innerText is the rendered form (block boundaries become newlines, which
  // norm() collapses to spaces; inline children stay unseparated, matching
  // accname). It is empty for hidden elements by definition, so sr-only labels
  // fall back to textContent rather than losing their name entirely.
  function accText(el) {
    if (!el) return "";
    var t = "";
    try { t = norm(el.innerText); } catch (e) {}
    if (!t) { try { t = norm(el.textContent); } catch (e) {} }
    return t;
  }

  // Accessible name via the design-ordered subset. Returns {name, source}.
  function accessibleName(el, doc) {
    var role = implicitRole(el);
    var tag = lc(el.tagName);

    // 1. <label for=id>
    if (el.id) {
      try {
        var labels = doc.querySelectorAll('label[for="' + CSS.escape(el.id) + '"]');
        if (labels && labels.length) {
          var lt = accText(labels[0]);
          if (lt) return { name: lt, source: "label-for" };
        }
      } catch (e) {}
    }
    // 2. aria-labelledby
    var lb = attr(el, "aria-labelledby");
    if (lb) {
      var parts = [];
      var ids = lb.split(/\s+/);
      for (var i = 0; i < ids.length; i++) {
        var t = idText(doc, ids[i]);
        if (t) parts.push(t);
      }
      var joined = norm(parts.join(" "));
      if (joined) return { name: joined, source: "aria-labelledby" };
    }
    // 3. aria-label
    var al = norm(attr(el, "aria-label"));
    if (al) return { name: al, source: "aria-label" };
    // 4. wrapping <label>
    try {
      var wrap = el.closest ? el.closest("label") : null;
      if (wrap) {
        var wt = accText(wrap);
        if (wt) return { name: wt, source: "wrapping-label" };
      }
    } catch (e) {}
    // 5. name-from-content (buttons / links / menuitems …)
    if (nameFromContentRole(role, tag)) {
      var ct = accText(el);
      if (!ct) {
        var vlabel = norm(el.value);        // input[type=submit] value
        if (vlabel) ct = vlabel;
      }
      if (ct) return { name: ct, source: "content" };
    }
    // 6. title (best-effort)
    var ti = norm(attr(el, "title"));
    if (ti) return { name: ti, source: "title" };
    // 7. placeholder (best-effort)
    var ph = norm(attr(el, "placeholder"));
    if (ph) return { name: ph, source: "placeholder" };
    return { name: "", source: "none" };
  }

  // The listbox a custom combobox owns/controls, if present in the DOM (aria-controls /
  // aria-owns id-ref first, else a descendant [role=listbox]). Read-only.
  function resolveListbox(el) {
    try {
      var ref = norm(attr(el, "aria-controls")) || norm(attr(el, "aria-owns"));
      var ids = ref ? ref.split(/\s+/) : [];
      for (var i = 0; i < ids.length; i++) {
        if (ids[i]) { var t = document.getElementById(ids[i]); if (t) return t; }
      }
      if (el.getAttribute && lc(el.getAttribute("role")) === "listbox") return el;
      if (el.querySelector) { var d = el.querySelector('[role="listbox"]'); if (d) return d; }
    } catch (e) {}
    return null;
  }

  // Option LABELS for a control — never values, never locators. Native <select> reads its
  // <option>s; a custom ARIA combobox reads [role=option] LABELS from the listbox it owns,
  // WHEN that listbox is present in the DOM (incl. display:none). A widget that builds its
  // options only on open yields [] here — the crawler's open-probe handles that case.
  //
  // (A one-line `optionsOf(el)` wrapper returning just `.list` used to sit here. Nothing
  // ever called it — `describe()` reads optionsAndTotalOf directly, because it needs the
  // total as well as the list — so it was dead code that read as a second option path and
  // permanently capped achievable coverage. Removed; the contract it documented lives on
  // the function that implements it, immediately below.)

  // {list, total} — the captured labels AND how many the control actually offers.
  // The two differ only when MAX_OPTIONS clipped the read, and that difference is
  // the whole point: a consumer can then say "247 options, first 300 captured"
  // instead of presenting a clipped list as the complete set of answers.
  function optionsAndTotalOf(el) {
    var out = [];
    var total = 0;
    try {
      if (lc(el.tagName) === "select") {
        var opts = el.options || [];
        for (var i = 0; i < opts.length; i++) {
          var t = norm(opts[i].textContent) || norm(opts[i].value);
          if (!t) continue;
          total++;
          if (out.length < MAX_OPTIONS) out.push(clip(t, MAX_OPTION));
        }
        return { list: out, total: total };
      }
      var role = lc(attr(el, "role"));
      var isChoice = role === "combobox" || role === "listbox" || !!norm(attr(el, "aria-haspopup"));
      if (isChoice) {
        var lb = resolveListbox(el);
        if (lb) {
          var nodes = lb.querySelectorAll ? lb.querySelectorAll('[role="option"]') : [];
          for (var j = 0; j < nodes.length; j++) {
            var ot = norm(nodes[j].textContent);
            if (!ot) continue;
            total++;
            if (out.length < MAX_OPTIONS) out.push(clip(ot, MAX_OPTION));
          }
        }
      }
    } catch (e) {}
    return { list: out, total: total };
  }

  function valueCommitted(el) {
    try {
      var tag = lc(el.tagName);
      var type = lc(el.type || "");
      if (tag === "input" && type === "password") return "";     // never captured
      if (tag === "input" && (type === "checkbox" || type === "radio")) {
        return el.checked ? "true" : "false";
      }
      if (tag === "select" || tag === "textarea" || tag === "input") {
        return clip(el.value == null ? "" : el.value, MAX_VALUE);
      }
      if (el.isContentEditable) return clip(norm(el.textContent), MAX_VALUE);
      // A CUSTOM CHOICE TRIGGER holds its value as rendered TEXT, not as a
      // value property — it is a <button>, so every branch above returns "".
      // Radix/shadcn, MUI and Headless UI all render the selection inside the
      // trigger, and the accessible NAME is not it: shadcn labels the trigger
      // with a <FormLabel>, so the name stays "Gender" while the content
      // becomes "Male". Nothing captured reflected the selection at all, which
      // (a) made a filled form read back as empty — the form_snapshot said
      // Gender:"" even when a human had chosen one — and (b) made an automated
      // fill impossible to verify, so every correct selection was discarded.
      var role = lc(attr(el, "role"));
      var pop = lc(attr(el, "aria-haspopup"));
      // A CUSTOM TOGGLE holds its state in ARIA, not in a value property. A
      // <button role="radio"> or role="checkbox" is not a form element, so every
      // branch above returns "" — the same hole v8 closed for choice triggers,
      // one widget class on. aria-checked / aria-pressed is the W3C-standard
      // answer here, so this reads a specification rather than guessing at any
      // one component library's markup.
      //
      // Mirrors the native branch exactly ("true"/"false"), so a custom toggle
      // and an <input type=checkbox> are indistinguishable downstream — which is
      // the point: the fill, the snapshot and the catalogue should not care which
      // one an application happened to use.
      var ariaState = lc(attr(el, "aria-checked")) || lc(attr(el, "aria-pressed"));
      if (role === "radio" || role === "checkbox" || role === "switch" ||
          role === "menuitemcheckbox" || role === "menuitemradio" ||
          (ariaState && (ariaState === "true" || ariaState === "false"))) {
        if (ariaState === "true") return "true";
        if (ariaState === "false") return "false";
        // "mixed" (a tri-state checkbox) is a real ARIA value and is neither;
        // reporting it as either would be a fabricated answer.
        if (ariaState === "mixed") return "mixed";
        return "";
      }
      if (role === "combobox" || pop === "listbox" || pop === "menu") {
        // A PLACEHOLDER IS NOT A VALUE. Radix marks the un-selected trigger
        // with data-placeholder; reading its text would record "Select" as the
        // committed answer, which is worse than recording nothing — the form
        // would look filled while the app still considers it empty.
        //
        // KNOWN GAP (v9). This marker is RADIX-SPECIFIC. An un-selected MUI or
        // Headless UI trigger carries no data-placeholder, so its rendered
        // "Select…" text WILL be recorded as a committed value on those stacks.
        // Deliberately not fixed from theory: the correct signal differs per
        // library and guessing it is how the last three attempts at this widget
        // went wrong. Read the un-selected DOM of whichever library a client app
        // actually uses, then add its marker here. Until then the gap is a
        // known, written-down limitation rather than a silent wrong value.
        if (el.hasAttribute("data-placeholder")) return "";
        return clip(norm(el.textContent), MAX_VALUE);
      }
      return "";
    } catch (e) { return ""; }
  }

  // The link DESTINATION (diagnostic). `el.href` (the IDL property) resolves a
  // relative/hash/routerLink href to an absolute URL; the raw attribute is the
  // fallback. Only for anchors — lets the crawler FOLLOW routes directly instead
  // of relying on a click producing an observable url change (pushState SPAs).
  function hrefOf(el) {
    try {
      if (lc(el.tagName) !== "a") return "";
      var h = el.href || attr(el, "href") || "";
      return clip("" + h, MAX_VALUE);
    } catch (e) { return ""; }
  }

  function isRequired(el) {
    try {
      if (el.required === true) return true;
      return lc(attr(el, "aria-required")) === "true";
    } catch (e) { return false; }
  }
  function isDisabled(el) {
    try {
      if (el.disabled === true) return true;
      return lc(attr(el, "aria-disabled")) === "true";
    } catch (e) { return false; }
  }

  function testId(el) {
    return attr(el, "data-testid") || attr(el, "data-test") ||
           attr(el, "data-cy") || attr(el, "data-qa") || "";
  }

  function cssHint(el) {
    try {
      var s = lc(el.tagName);
      if (el.id) s += "#" + el.id;
      var cls = norm(el.className && el.className.baseVal !== undefined
        ? el.className.baseVal : el.className);
      if (cls) {
        var parts = cls.split(" ").filter(Boolean).slice(0, 3);
        if (parts.length) s += "." + parts.join(".");
      }
      return clip(s, 200);
    } catch (e) { return ""; }
  }

  // Nearest landmark ancestor → {role, name}. Crosses shadow host boundaries.
  var LANDMARK = {
    "tr": "row", "li": "listitem", "article": "article", "section": "region",
    "fieldset": "group", "td": "cell", "th": "cell", "dialog": "dialog",
    "ul": "list", "ol": "list"
  };
  var LANDMARK_ROLE = {
    "row": "row", "listitem": "listitem", "article": "article",
    "region": "region", "group": "group", "gridcell": "gridcell",
    "cell": "cell", "list": "list", "listbox": "listbox", "option": "option",
    "menuitem": "menuitem", "menu": "menu", "tabpanel": "tabpanel",
    "dialog": "dialog", "form": "form", "table": "table", "rowgroup": "rowgroup"
  };

  function landmarkRoleOf(el) {
    var explicit = lc(attr(el, "role"));
    if (explicit && LANDMARK_ROLE[explicit]) return LANDMARK_ROLE[explicit];
    var tag = lc(el.tagName);
    return LANDMARK[tag] || "";
  }

  function landmarkName(el, doc) {
    var an = accessibleName(el, doc);
    if (an.name) return clip(an.name, MAX_LANDMARK);
    // heading inside, else first short text line
    try {
      var h = el.querySelector("h1,h2,h3,h4,h5,h6,[role=heading]");
      if (h) { var ht = norm(h.textContent); if (ht) return clip(ht, MAX_LANDMARK); }
    } catch (e) {}
    var t = norm(el.textContent);
    return clip(t, MAX_LANDMARK);
  }

  function parentAcross(el) {
    if (el.parentElement) return el.parentElement;
    try {
      var root = el.getRootNode ? el.getRootNode() : null;
      if (root && root.host) return root.host;      // step out of a shadow root
    } catch (e) {}
    return null;
  }

  // The mutually-exclusive CHOICE GROUP a control belongs to, or "" when it is
  // not part of one.  This is structure, never a value: it answers "which
  // question does this answer belong to", not "what did anyone answer".
  //
  // HTML already defines the grouping for native radios — same ``name`` inside
  // the same form is one group — and ARIA defines it with an ancestor
  // ``role=radiogroup``.  Reading it here is what lets the inventory present ONE
  // decision with N options instead of N unrelated toggles, and what lets a
  // planned walk force exactly one option without guessing which sibling owns it.
  //
  // Scoped to the owning form, because two forms on a page may each use
  // ``name="product"`` and they are NOT the same question.
  // A stable discriminator for the ROOT a group container lives in.
  //
  // "" for the main document, so every light-DOM group key stays BYTE-IDENTICAL
  // to what it has always been. That is not cosmetic: group_id hashes key the
  // remembered branch-walk overrides a previous crawl recorded, and re-hashing
  // them would silently orphan every plan (see app/inventory.py pass 3).
  //
  // For a shadow root it is the host chain. Without it, container keys are only
  // unique WITHIN a root — two unlabelled radiogroups in two different shadow
  // roots both key "ix:0" and merge into one question offering the other's
  // answers, and an id-keyed container collides with an identically-id'd one
  // outside the root, which is the very thing shadow encapsulation exists to
  // allow.
  function rootKey(node) {
    var out = "", cur = node, hops = 0;
    try {
      while (cur && cur.host && hops < 8) {
        var h = cur.host;
        var seg = lc(h.tagName) + (h.id ? "#" + h.id : "");
        var p = h.parentElement;
        if (p && p.children) {
          for (var i = 0; i < p.children.length; i++) {
            if (p.children[i] === h) { seg += ":" + i; break; }
          }
        }
        out = out ? (seg + ">" + out) : seg;
        cur = h.getRootNode ? h.getRootNode() : null;
        hops++;
      }
    } catch (e) {}
    return out;
  }

  function groupContainerKey(cur, doc) {
    var rk = rootKey(doc);
    var pre = rk ? (rk + "|") : "";
    var id = attr(cur, "id");
    if (id) return pre + "id:" + id;
    var al = attr(cur, "aria-label");
    if (al) return pre + "al:" + lc(al);
    var lb = attr(cur, "aria-labelledby");
    if (lb) return pre + "lb:" + lb;
    try {                                  // positional, stable within a root
      var all = doc.querySelectorAll("[role=radiogroup],fieldset");
      for (var i = 0; i < all.length; i++) { if (all[i] === cur) return pre + "ix:" + i; }
    } catch (e) {}
    return "";
  }

  // ── THE QUESTION A CONTROL ANSWERS, IN THE APPLICATION'S OWN WORDS ────────
  //
  // A control's accessible name names the ANSWER ("Yes", "Male", "Term 20"),
  // never the question. The question lives on the grouping container the DOM
  // already declares — a <legend>, a radiogroup's aria-label, a heading inside
  // a role=group — and nothing has ever read it. Downstream that left the
  // catalogue with the only wording it had: the answers themselves, and for a
  // bare-button questionnaire a fabricated "Question 1", "Question 2".
  //
  // DECLARED SOURCES ONLY, strongest first, and every one of them is an
  // accessible-name rung the W3C already defines for a group:
  //   1. aria-labelledby on the container  → resolved rendered text
  //   2. aria-label on the container
  //   3. <legend> — the HTML element whose entire purpose is to caption a
  //      <fieldset>, i.e. to state the question its controls answer
  //   4. a heading inside the container (h1-h6 / role=heading) — how a design
  //      system that cannot use <legend> states the same thing
  //
  // NEVER proximity, never "the text just above". A question inferred from
  // layout is a question the application never asked, and a catalogue that
  // invents wording is worse than one that admits it has none: the whole point
  // of this field is that a reader can trust what it says. "" when the page
  // declared nothing, and the caller records UNVERIFIED rather than guessing.
  function questionLabelOf(container, doc) {
    if (!container) return { label: "", source: "" };
    try {
      var lb = attr(container, "aria-labelledby");
      if (lb) {
        var parts = [], ids = lb.split(/\s+/);
        for (var i = 0; i < ids.length; i++) {
          var t = idText(doc, ids[i]);
          if (t) parts.push(t);
        }
        var joined = norm(parts.join(" "));
        if (joined) return { label: clip(joined, MAX_NAME), source: "aria-labelledby" };
      }
      var al = norm(attr(container, "aria-label"));
      if (al) return { label: clip(al, MAX_NAME), source: "aria-label" };
      if (lc(container.tagName) === "fieldset") {
        // The FIRST legend that belongs to THIS fieldset. A nested fieldset's
        // legend captions its own question, and querySelector would return it
        // for the outer one too — which would label a question with a
        // sub-question's wording.
        for (var c = container.firstElementChild; c; c = c.nextElementSibling) {
          if (lc(c.tagName) === "legend") {
            var lt = accText(c);
            if (lt) return { label: clip(lt, MAX_NAME), source: "legend" };
            break;
          }
        }
      }
      var h = container.querySelector("h1,h2,h3,h4,h5,h6,[role=heading],legend");
      if (h) {
        var ht = accText(h);
        if (ht) return { label: clip(ht, MAX_NAME), source: "heading" };
      }
    } catch (e) {}
    return { label: "", source: "" };
  }

  // The nearest DECLARED question container above a control, or null.
  //
  // Wider than groupKeyOf's radio/checkbox scope on purpose: the questions that
  // most needed real wording are rendered as bare <button>s ("Yes"/"No" pairs on
  // a health questionnaire), which are not radios, carry no name attribute, and
  // therefore never entered the grouping logic at all.
  function questionContainerOf(el) {
    var cur = parentAcross(el), hops = 0;
    while (cur && cur.nodeType === 1 && hops < 12) {
      var r = lc(attr(cur, "role"));
      if (r === "radiogroup" || r === "group"
          || lc(cur.tagName) === "fieldset") {
        return cur;
      }
      cur = parentAcross(cur);
      hops++;
    }
    return null;
  }

  function questionOf(el, doc) {
    try {
      var container = questionContainerOf(el);
      if (!container) return { key: "", label: "", source: "" };
      var key = groupContainerKey(container, doc);
      var q = questionLabelOf(container, doc);
      return { key: key ? ("q:" + key) : "", label: q.label, source: q.source };
    } catch (e) { return { key: "", label: "", source: "" }; }
  }

  function groupKeyOf(el, doc) {
    try {
      var tag = lc(el.tagName);
      var it = lc(el.type || "");
      var role = lc(attr(el, "role"));
      var isRadio = (tag === "input" && it === "radio") || role === "radio";
      // A CHECKBOX GROUP IS A QUESTION TOO. "Health Conditions — pick at least
      // one of eight" is ONE question, and recording it as eight independent
      // yes/no questions describes a form the application does not have.
      //
      // Grouped ONLY on a DECLARED signal — a shared name attribute (the
      // classic name="conditions[]" array every server-side framework renders)
      // or an explicit fieldset / role=group. Never on mere proximity: a
      // "Remember me" sitting beside a "Subscribe to newsletter" is two
      // questions, and merging them would answer one and silently drop the
      // other from the residue. An undeclared group stays exactly as it was.
      var isCheck = (tag === "input" && it === "checkbox") || role === "checkbox";
      if (!isRadio && !isCheck) return "";
      if (tag === "input") {
        var n = attr(el, "name");
        if (n) {
          var f = el.form;
          var fid = f ? (attr(f, "id") || attr(f, "name") || "f") : "doc";
          return "name:" + fid + ":" + n;
        }
      }
      // No usable name attribute (ARIA card sets, nameless radios): fall back to
      // the nearest declared grouping container.
      var cur = parentAcross(el);
      var hops = 0;
      while (cur && cur.nodeType === 1 && hops < 12) {
        var r = lc(attr(cur, "role"));
        if (r === "radiogroup" || lc(cur.tagName) === "fieldset"
            || (isCheck && r === "group")) {
          var k = groupContainerKey(cur, doc);
          if (k) return "grp:" + k;
        }
        cur = parentAcross(cur);
        hops++;
      }
    } catch (e) {}
    return "";                              // ungrouped → behaves exactly as before
  }

  // ── VALIDITY, SCOPED TO THE CONTROL THAT OWNS IT ─────────────────────────
  //
  // Nothing here was ever captured, which is why validity had to be read from
  // the PAGE: `error_texts()` returns every visible [role=alert] on the
  // document, so a cookie banner marked role=alert (they nearly all are, so a
  // screen reader announces them) failed every fill on the page, and one real
  // error on field 3 failed fields 4 through 12 as well.
  //
  // These three attributes are the accessibility contract for exactly this
  // question, and every form library in common use already emits them.
  //
  // VALUE-FREE: an error MESSAGE is product UI text, the same class of string as
  // a label or an option — it says what the application demands, never what
  // anybody entered.
  function errorTextFor(el, doc) {
    var ids = (attr(el, "aria-errormessage") + " " +
               attr(el, "aria-describedby")).split(/\s+/);
    var out = [];
    for (var i = 0; i < ids.length && out.length < 2; i++) {
      if (!ids[i]) continue;
      try {
        var node = doc.getElementById(ids[i]);
        if (!node || !isVisible(node)) continue;
        var t = norm(node.textContent);
        if (t) out.push(t);
      } catch (e) {}
    }
    if (!out.length) {
      // The convention every form library falls back to when it does not wire
      // aria-describedby: an error node whose id is the field id plus a suffix.
      var base = attr(el, "id");
      var SUFFIXES = ["-error", "_error", "-err", "_err", "-error-message",
                      "-helper-text", "-validation"];
      for (var j = 0; j < SUFFIXES.length && !out.length; j++) {
        try {
          var n2 = doc.getElementById(base + SUFFIXES[j]);
          if (n2 && isVisible(n2)) {
            var t2 = norm(n2.textContent);
            if (t2) out.push(t2);
          }
        } catch (e) {}
      }
    }
    return clip(out.join(" "), MAX_LANDMARK);
  }

  // The browser's OWN verdict on the value the control currently holds.  Free,
  // exact, and the strongest signal there is — it is the constraint the browser
  // will itself enforce on submit, in the browser's own words.
  function nativeValidationMessage(el) {
    try {
      if (typeof el.checkValidity !== "function") return "";
      if (el.checkValidity()) return "";
      return clip(norm(el.validationMessage || ""), MAX_LANDMARK);
    } catch (e) { return ""; }
  }

  // The SECTION heading a control sits under.
  //
  // A real application labels the group once — "Beneficiary Information" — and
  // then labels the fields inside it plainly: "First Name", "Date of Birth".
  // Read only the control's own name and every one of them belongs to the
  // applicant, which is exactly how a beneficiary came to be filled with the
  // insured.  The nearest landmark already computes this; it was simply thrown
  // away unless two controls collided.
  //
  // Product UI text, never a value — the same discipline as a label.
  function sectionOf(el, doc) {
    var lm = nearestLandmark(el, doc);
    return lm && lm.name ? clip(lm.name, MAX_LANDMARK) : "";
  }

  // IS THIS CONTROL IN A LIST'S FILTER ROW?
  //
  // A data table declares the difference itself: the filter row lives in
  // <thead> and the data lives in <tbody>. MEASURED (Dolibarr, 2026-08-29):
  // a list page's filter inputs were catalogued as business fields the client
  // had to supply values for -- "Third parties with sales representative",
  // "Cust./Prosp. tags/categories". Structure, never vocabulary: "search" is a
  // word in one language and this crawls applications in many.
  function filterScopeOf(el) {
    var cur = el;
    var hops = 0;
    while (cur && cur.nodeType === 1 && hops < 30) {
      var tag = lc(cur.tagName);
      if (tag === "thead") return "thead";
      if (tag === "tbody" || tag === "tfoot") return tag;
      if (tag === "form" || tag === "body") return "";
      cur = parentAcross(cur);
      hops++;
    }
    return "";
  }

  function nearestLandmark(el, doc) {
    var cur = parentAcross(el);
    var hops = 0;
    while (cur && cur.nodeType === 1 && hops < 40) {
      var role = landmarkRoleOf(cur);
      if (role) {
        return { role: role, name: landmarkName(cur, cur.ownerDocument || doc) };
      }
      cur = parentAcross(cur);
      hops++;
    }
    return { role: "", name: "" };
  }

__SHADOW_HOOK_JS__
__FRAME_SELECTOR_JS__

  // ---- disclosure state ----------------------------------------------------

  // Is this control a CLOSED door in front of content the page is not currently
  // showing — an accordion header, a <details>, an unselected tab?  (M2.6 /
  // T-CAP-03.)
  //
  // WHY CAPTURE HAS TO ANSWER THIS. `isVisible` correctly refuses to catalogue
  // a control inside a collapsed accordion or a closed <details>: it is not on
  // the page, and cataloguing it would be a capture-says-covered /
  // replay-cannot-bind claim. But that leaves the field genuinely uncatalogued —
  // a question the application asks that no crawl of it ever recorded. The
  // crawler's answer is to OPEN the door and read again, and the only way for
  // that to be a deliberate act rather than blind clicking is for it to know,
  // from the DOM itself, which controls are doors and which of those are shut.
  //
  // Three declarations, in the order of how much they mean:
  //
  //   1. <details>/<summary> — `open` is a live PROPERTY, not an attribute that
  //      tracks state, so it cannot be read with attr(). This is the case that
  //      forced a capture change: nothing already emitted distinguishes an open
  //      <details> from a closed one, and clicking a <summary> to find out
  //      CLOSES the ones that were already open.
  //   2. aria-expanded — the ARIA disclosure contract (also already emitted raw
  //      as `expanded`; normalised here so one field answers the question).
  //   3. role=tab + aria-selected — a tab panel is a disclosure whose "closed"
  //      state is spelled differently.
  //
  // "" means "not a disclosure control", and is the answer for the overwhelming
  // majority of controls. NOTHING is inferred from class names or from the
  // shape of the DOM: a heuristic here would turn into a blind click.
  function disclosureState(el) {
    try {
      var tag = lc(el.tagName);
      if (tag === "summary") {
        var host = el.parentNode;
        if (host && lc(host.tagName || "") === "details") {
          return host.open ? "expanded" : "collapsed";
        }
        return "";
      }
      var exp = lc(attr(el, "aria-expanded"));
      if (exp === "true") return "expanded";
      if (exp === "false") return "collapsed";
      if (lc(attr(el, "role")) === "tab") {
        var sel = lc(attr(el, "aria-selected"));
        if (sel === "true") return "expanded";
        if (sel === "false") return "collapsed";
      }
      return "";
    } catch (e) { return ""; }
  }

  // ---- collection ----------------------------------------------------------

  function describe(el, doc, frameSelector) {
    var role = implicitRole(el);
    var an = accessibleName(el, doc);
    var tag = lc(el.tagName);
    var type = lc(el.type || "");
    var best = an.source === "title" || an.source === "placeholder";
    var opt = optionsAndTotalOf(el);
    var qOf = questionOf(el, doc);
    return {
      role: role,
      name: clip(an.name, MAX_NAME),
      name_source: an.source,
      best_effort: best,
      kind: role || tag,               // naive; app.inventory refines
      tag: tag,
      input_type: type,
      // ---- WHAT THE APPLICATION DECLARED THIS FIELD IS FOR --------------------
      // The deterministic classifier (app/field_semantics.py) ranks `autocomplete`
      // FIRST, above every reading of a label, because it is a W3C-standard
      // vocabulary: when an app sets it, the app has NAMED the field's semantics
      // itself. That rung was unreachable — capture never emitted the attribute,
      // so classify() could only ever fall through to the weakest name-token rung
      // and a field labelled "Field A7" classified to nothing at all.
      //
      // Enumerated keyword attributes are case-insensitive per spec, so they are
      // normalised here the same way `role` and `aria-haspopup` already are.
      autocomplete: lc(attr(el, "autocomplete")),
      inputmode: lc(attr(el, "inputmode")),
      // `placeholder` and `id` are the classifier's fallbacks for a control with
      // NO accessible name: field_signature.compute() tokenises the placeholder,
      // then the id, before giving up. Both were dead code. `placeholder` is
      // human-authored text and `id` is a case-sensitive identifier (it has to
      // round-trip through getElementById and CSS.escape), so both are preserved
      // VERBATIM rather than normalised.
      placeholder: clip(attr(el, "placeholder"), MAX_NAME),
      id: clip(attr(el, "id"), MAX_NAME),
      options: opt.list,
      // How many options the control ACTUALLY offers. Equal to options.length
      // unless MAX_OPTIONS clipped the read — the honest signal that the captured
      // list is a prefix, so a truncated enumeration is never catalogued as the
      // complete set of answers to the question.
      options_total: opt.total,
      // Which mutually-exclusive question this control answers ("" if none).
      group_key: groupKeyOf(el, doc),
      // WHICH QUESTION THIS CONTROL ANSWERS, AND HOW THE PAGE WORDED IT.
      // ``question_key`` identifies the declared container (so two controls in
      // one <fieldset> are two answers to ONE question, even when they are bare
      // buttons the grouping logic above cannot see); ``question_label`` is the
      // application's own wording for it, from a declared accessible-name rung
      // only. Both "" when the page declared no question container — the honest
      // answer, and the one that keeps a fabricated "Question 3" out of the
      // catalogue.
      question_key: qOf.key,
      question_label: qOf.label,
      question_label_source: qOf.source,
      required: isRequired(el),
      disabled: isDisabled(el),
      frame_selector: frameSelector || "",
      testid: testId(el),
      css_hint: cssHint(el),
      value_committed: valueCommitted(el),
      href: hrefOf(el),
      haspopup: lc(attr(el, "aria-haspopup")),
      expanded: lc(attr(el, "aria-expanded")),
      // "collapsed" | "expanded" | "" — the DOM's own declaration that this
      // control is a door, and whether it is shut. Drives the crawler's
      // deliberate pre-capture expansion pass (M2.6 / T-CAP-03); see
      // `disclosureState` above for why aria-expanded alone was not enough.
      disclosure: disclosureState(el),
      // Toggle-button selection state. A custom questionnaire renders each answer
      // as a <button> (not a radio), and marks the chosen one with aria-pressed /
      // aria-checked — the ONLY signal that a button IS an answer and whether it is
      // the selected one. Without it a "Yes"/"No" answer button is indistinguishable
      // from a plain action button.
      pressed: lc(attr(el, "aria-pressed")),
      aria_checked: lc(attr(el, "aria-checked")),
      // Declared value constraints (number/range/date inputs). The DOM's OWN
      // truth — the default synthesizer uses them so an auto-filled value can
      // never violate the app's min/max/step validation (a constraint-blind
      // "1" in an <input min=18> silently voids the whole submit).
      min: attr(el, "min") || "",
      max: attr(el, "max") || "",
      step: attr(el, "step") || "",
      // THE REST OF THE RULE THE APPLICATION DECLARED ABOUT ITSELF. A catalogue
      // question with no validation justifies no negative and no boundary case,
      // so a crawl that reads only min/max/step leaves the scenario deriver
      // nothing to work with on every text field in the fleet.
      pattern: attr(el, "pattern") || "",
      minlength: attr(el, "minlength") || "",
      maxlength: attr(el, "maxlength") || "",
      // Drag-and-drop signal (HTML5 draggable / ARIA grab-drop) — no interaction
      // primitive yet, so the matcher names it UNHANDLED for the coverage ledger
      // rather than silently skipping it.
      //
      // The absence test MUST compare against "" and not null/undefined: attr()
      // returns "" for a missing attribute (`getAttribute(n) || ""`), so
      // `!== null && !== undefined` was true for EVERY element ever inspected.
      // That marked every control drag-and-drop, and the matcher's drag rule runs
      // BEFORE the affordance rule — so every link, button and field resolved to
      // UNHANDLED and the crawler refused to click anything. A crawl would land on
      // its entry page, read the form, and report one visit with every nav link
      // ledgered as "drag-drop (no interaction primitive yet)".
      draggable: (attr(el, "draggable") === "true") ||
                 (attr(el, "aria-grabbed") !== ""),
      roledescription: lc(attr(el, "aria-roledescription")),
      // CONTROL-SCOPED VALIDITY (see errorTextFor / nativeValidationMessage).
      aria_invalid: lc(attr(el, "aria-invalid")),
      aria_describedby: attr(el, "aria-describedby"),
      aria_errormessage: attr(el, "aria-errormessage"),
      error_text: errorTextFor(el, doc),
      validation_message: nativeValidationMessage(el),
      // POSSESSOR CONTEXT (see sectionOf).
      section: sectionOf(el, doc),
      landmark: nearestLandmark(el, doc),
      // "thead" when the control sits in a table's filter row.
      filter_scope: filterScopeOf(el)
    };
  }

  // Walk a document/shadow-root subtree; recurse open shadow roots + iframes.
  //
  // ``shadowScope`` is "" for everything reachable by an ordinary locator, and
  // "closed_shadow" once the walk is inside a CLOSED root (M3.2 / T-FR-02).
  // THAT DISTINCTION IS NOT DECORATION.  Playwright's selector engine pierces
  // OPEN shadow roots by reading `element.shadowRoot`, which the spec keeps null
  // for a closed one — for Playwright exactly as for the page.  So a control in
  // there can now be OBSERVED and catalogued, and still cannot be bound by
  // `getByRole`, `getByLabel` or CSS.  Carrying the fact with the control is
  // what stops "we catalogued it" being read as "we can act on it", which is
  // precisely the capture-says-covered / replay-cannot-bind claim this engine's
  // browser harness exists to catch.  A nested OPEN root inside a closed one
  // inherits the scope, because it is only reachable through the closed one.
  function walk(root, doc, frameSelector, sink, seenDocs, shadowScope) {
    var nodes;
    try { nodes = root.querySelectorAll(SELECTOR); } catch (e) { return; }
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!isVisible(el)) continue;
      try {
        var rec = describe(el, doc, frameSelector);
        if (shadowScope) rec.shadow_scope = shadowScope;
        sink.push(rec);
      } catch (e) {}
    }
    // open shadow roots (same frame → no selector change)
    //
    // The shadow root is passed as its OWN resolution root, not the outer
    // document. An id reference does not cross a shadow boundary: `label[for]`
    // and `aria-labelledby` are resolved against the node's root, which is how a
    // browser, an assistive technology and Playwright's getByRole all resolve
    // them. Forwarding `doc` here ran those lookups against the host document,
    // where the shadow-scoped ids do not exist — so every labelled control in a
    // shadow root came back name:"" and, because the compiler binds by accessible
    // name only, was dropped from the generated script entirely. A shadow-DOM
    // design system (Lightning, Vaadin, any lit app) presented as a page with no
    // fillable fields.
    //
    // ShadowRoot implements getElementById and querySelectorAll, so it is a
    // drop-in root here — and scoping to it is also what keeps an identically-id'd
    // element OUTSIDE the shadow root from being picked up by mistake.
    //
    // M3.2 / T-FR-02 — and CLOSED shadow roots, when the capture init script was
    // in place before the component constructed itself. `shadowRootOf` returns
    // the open root when there is one and otherwise asks the hook; with no hook
    // installed it returns null and this loop descends exactly what it always
    // descended. The closed root is walked as its OWN resolution root for the
    // same id-scoping reason as the open one.
    var all;
    try { all = root.querySelectorAll("*"); } catch (e) { all = []; }
    for (var j = 0; j < all.length; j++) {
      var host = all[j];
      var hostRoot = shadowRootOf(host);
      if (hostRoot) {
        var openHere = false;
        try { openHere = !!host.shadowRoot; } catch (e) { openHere = false; }
        walk(hostRoot, hostRoot, frameSelector, sink, seenDocs,
             openHere ? shadowScope : "closed_shadow");
      }
    }
    // same-origin iframes (cross-origin access throws → skip honestly)
    var frames;
    try { frames = root.querySelectorAll("iframe"); } catch (e) { frames = []; }
    for (var k = 0; k < frames.length; k++) {
      var ifr = frames[k];
      var cdoc = null;
      try { cdoc = ifr.contentDocument; } catch (e) { cdoc = null; }
      if (cdoc && seenDocs.indexOf(cdoc) === -1) {
        seenDocs.push(cdoc);
        var childSel = frameSelectorFor(ifr, k);
        var sel = frameSelector ? (frameSelector + " >>> " + childSel) : childSel;
        try { walk(cdoc, cdoc, sel, sink, seenDocs, shadowScope); } catch (e) {}
      }
    }
  }

  var out = [];
  var seen = [document];
  walk(document, document, "", out, seen, "");
  return out;
})()
""".replace("__SHADOW_HOOK_JS__", _SHADOW_HOOK_JS)
  .replace("__FRAME_SELECTOR_JS__", _FRAME_SELECTOR_JS)
  .replace("__MAX_OPTIONS__", str(MAX_OPTIONS)))


#: OPAQUE-SURFACE detector — positively FINDS the surfaces the DOM walker cannot read, so a
#: blind spot becomes a named ledger row instead of an empty "clean" scan. Detects (a)
#: cross-origin iframes (Stripe/reCAPTCHA/maps/Plaid), (b) large canvas-rendered UIs
#: (Flutter/WebGL/charts), (c) custom elements rendering via a CLOSED shadow root (heuristic:
#: a dash-tagged element with size but no readable light DOM). Labels/kinds only, never a
#: fabricated capture — the honest anti-green-wash of coverage.
OPAQUE_JS = (r"""
(function () {
  var out = [], MAXO = 40, seen = {};
__SHADOW_HOOK_JS__
__FRAME_SELECTOR_JS__
  function vis(el){ try { var r = el.getBoundingClientRect(); var s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden"
      && parseFloat(s.opacity || "1") > 0; } catch (e) { return false; } }
  // THE DEDUP KEY IS WHAT MAKES TWO SURFACES TWO SURFACES.
  //
  // `kind|label` was fine while a label identified a surface, and a frame's
  // label is its HOST. A checkout that embeds a card frame and a 3-D-Secure
  // frame from the same vendor is two frames on one host, so the second was
  // silently dropped — named nowhere, entered never, and indistinguishable in
  // the ledger from a page that only had one. When a row carries a frame
  // selector, THAT is its identity.
  function push(kind, label, reason, extra){
    var key = kind + "|" + ((extra && extra.frame_selector) || label);
    if (out.length < MAXO && !seen[key]) { seen[key] = 1;
      var row = { kind: kind, label: ("" + label).slice(0, 160), reason: reason };
      if (extra) { for (var k2 in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, k2)) row[k2] = extra[k2]; } }
      out.push(row); } }
  try {
    // M3.2 / T-FR-01 — the frame set is the SHADOW-PIERCING, document-wide one,
    // so the ordinal in a positional `frame_selector` means the same thing here
    // as it does when Playwright resolves it. Each row now carries the escaped,
    // deterministic selector the port needs to ENTER the frame; before this, the
    // row named a host and nothing could act on it, which is why
    // `route_opaque_surfaces`'s `enter_frames` bucket had no consumer.
    var frames = framesOf(document);
    for (var i = 0; i < frames.length; i++) { var f = frames[i]; if (!vis(f)) continue;
      var readable = false; try { readable = !!f.contentDocument; } catch (e) { readable = false; }
      if (!readable) { var src = ""; try { src = f.getAttribute("src") || ""; } catch (e) {}
        var host = src; try { host = new URL(src, location.href).host; } catch (e) {}
        push("cross_origin_iframe", host || "embedded frame",
             "a cross-origin embed the DOM can't read (e.g. payment/captcha/map)",
             { frame_selector: frameSelectorFor(f, i), frame_host: host || "" }); } }
  } catch (e) {}
  try {
    var cs = document.querySelectorAll("canvas");
    for (var j = 0; j < cs.length; j++) { var c = cs[j]; if (!vis(c)) continue;
      var rc = c.getBoundingClientRect(); if (rc.width * rc.height < 40000) continue;
      push("canvas", (c.getAttribute("aria-label") || "canvas region"),
           "a canvas-rendered surface — no DOM controls to read (chart / Flutter / WebGL)"); }
  } catch (e) {}
  try {
    var all = document.getElementsByTagName("*");
    for (var k = 0; k < all.length && k < 5000 && out.length < MAXO; k++) { var el = all[k];
      var tag = (el.tagName || "").toLowerCase();
      if (tag.indexOf("-") === -1) continue;          // custom element only
      if (el.shadowRoot) continue;                     // open shadow — already walked
      // M3.2 / T-FR-02 — a CLOSED root the init script observed at construction
      // time is no longer a blind spot, and reporting it as one would understate
      // this crawl's coverage as badly as the reverse would overstate it. It
      // becomes a POSITIVE evidence row naming how many controls were read from
      // inside it; the surface is only `closed_shadow` when it is really opaque.
      var closed = closedRootOf(el);
      if (closed) {
        if (!vis(el)) continue;
        var n = 0;
        try { n = closed.querySelectorAll(
          "input,select,textarea,button,a[href],[role],[contenteditable]").length; }
        catch (e) { n = 0; }
        push("closed_shadow_entered", tag,
             "a <" + tag + "> closed shadow root, observed at construction time by the "
             + "capture init script — " + n + " control(s) read from inside it, "
             + "catalogued but NOT bindable by a standard locator (no selector "
             + "engine can reach into a closed root)",
             { controls_observed: n, controls_bindable: false });
        continue;
      }
      if (el.childElementCount > 0) continue;          // has light DOM we read
      if ((el.textContent || "").trim()) continue;     // has readable text
      if (!vis(el)) continue;
      var r = el.getBoundingClientRect(); if (r.height < 40) continue;
      push("closed_shadow", tag,
           "a <" + tag + "> element rendering via a closed shadow root the DOM can't pierce"); }
  } catch (e) {}
  return out;
})()
""".replace("__SHADOW_HOOK_JS__", _SHADOW_HOOK_JS)
  .replace("__FRAME_SELECTOR_JS__", _FRAME_SELECTOR_JS))

#: Bumped when either injected snippet changes (traces a manifest to its JS gen).
DISPLAYED_VALUES_JS_VERSION = "disp-js-v1"

#: ANSWERS P1.B — a SEPARATE injected snippet (the interactive walker above is
#: untouched) that captures DISPLAYED VALUE nodes: a rendered output like
#: ``<div class="prem">$75.00</div>`` that no interactive-control walker sees. For
#: each it returns ``{label, selector, text}`` so the value oracle can ground an
#: expected outcome WITHOUT a client-authored source_hint. Selective by design (a
#: currency/amount/percent gate on the element's OWN text) to avoid capturing every
#: number on the page.
DISPLAYED_VALUES_JS = r"""
(() => {
  "use strict";
  var MAX = 200, MAX_TEXT = 120, MAX_LABEL = 120;
  function norm(s){return ((""+(s==null?"":s)).replace(/\s+/g," ")).trim();}
  function clip(s,n){s=""+(s==null?"":s);return s.length>n?s.slice(0,n):s;}
  function attr(el,n){try{return el.getAttribute(n)||"";}catch(e){return "";}}
  // Same ancestor-aware hiding as the inventory walker (see the long note on
  // hiddenByAncestor there). `display` is not inherited, so without this a
  // displayed VALUE was read out of a collapsed accordion or a closed <details>
  // and reported as on-screen — a value the user cannot actually see.
  function parentOrHost(n){var p=n.parentElement;if(p)return p;
    var r=n.parentNode;if(r&&r.nodeType===11&&r.host)return r.host;return null;}
  function hiddenByAncestor(el){
    var view=(el.ownerDocument&&el.ownerDocument.defaultView)||window;
    var node=parentOrHost(el),hops=0;
    while(node&&hops<200){
      if(node.hasAttribute("hidden"))return true;
      if(((node.getAttribute("aria-hidden")||"")+"").toLowerCase()==="true")return true;
      if(node.tagName==="DETAILS"&&!node.hasAttribute("open")){
        var inSum=false,p=el;
        while(p&&p!==node){if(p.tagName==="SUMMARY"){inSum=true;break;}p=parentOrHost(p);}
        if(!inSum)return true;}
      try{var s=view.getComputedStyle(node);if(s&&s.display==="none")return true;}catch(e){}
      node=parentOrHost(node);hops++;}
    return false;}
  function isVisible(el){try{if(!el||el.nodeType!==1)return false;
    if(el.hasAttribute("hidden"))return false;
    var st=(el.ownerDocument.defaultView||window).getComputedStyle(el);
    if(st){
      if(st.display==="none"||st.visibility==="hidden")return false;
      if(parseFloat(st.opacity||"1")===0)return false;}
    if(hiddenByAncestor(el))return false;return true;}catch(e){return true;}}
  // A value-looking string: currency, a 2-decimal amount, a thousands-grouped int, or a percent.
  var VALUE_RX = /[$€£]\s?\d[\d,]*(?:\.\d+)?|\b\d[\d,]*\.\d{2}\b|\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?\s?%/;
  // The element's OWN direct text (not descendants) so a whole container never matches.
  function ownText(el){var t="";var ch=el.childNodes;
    for(var i=0;i<ch.length;i++){if(ch[i].nodeType===3)t+=ch[i].nodeValue;}return norm(t);}
  function labelOf(el){
    var al=norm(attr(el,"aria-label")); if(al) return al;
    if(el.tagName==="DD"){var p=el.previousElementSibling;
      while(p&&p.tagName!=="DT")p=p.previousElementSibling;
      if(p){var dt=norm(p.textContent);if(dt)return dt;}}
    var lb=attr(el,"aria-labelledby");
    if(lb){try{var r=el.ownerDocument.getElementById(lb.split(" ")[0]);
      if(r){var rt=norm(r.textContent);if(rt&&!VALUE_RX.test(rt))return rt;}}catch(e){}}
    var sib=el.previousElementSibling,hops=0;
    while(sib&&hops<3){var stt=norm(sib.textContent);
      if(stt&&stt.length<=MAX_LABEL&&!VALUE_RX.test(stt))return stt;
      sib=sib.previousElementSibling;hops++;}
    var par=el.parentElement;
    if(par){var psib=par.previousElementSibling,h2=0;
      while(psib&&h2<2){var pt=norm(psib.textContent);
        if(pt&&pt.length<=MAX_LABEL&&!VALUE_RX.test(pt))return pt;
        psib=psib.previousElementSibling;h2++;}
      try{var lbl=par.querySelector("label,.muted,.label,dt,[class*=label]");
        if(lbl&&lbl!==el){var lt=norm(lbl.textContent);
          if(lt&&!VALUE_RX.test(lt))return clip(lt,MAX_LABEL);}}catch(e){}}
    return "";
  }
  function selectorFor(el){
    if(el.id)return "#"+el.id;
    var tag=el.tagName.toLowerCase();
    var cls=norm(el.className&&el.className.baseVal!==undefined?el.className.baseVal:el.className);
    if(cls){var first=cls.split(" ").filter(Boolean)[0];
      if(first){var sel=tag+"."+first;
        try{if(el.ownerDocument.querySelectorAll(sel).length===1)return sel;}catch(e){}
        return sel;}}
    return tag;
  }
  var out=[],seen={};
  var els;
  try{els=document.querySelectorAll("span,div,dd,output,td,strong,b,p,h1,h2,h3,h4,li,[data-testid],[class]");}
  catch(e){els=[];}
  for(var i=0;i<els.length&&out.length<MAX;i++){
    var el=els[i];
    var tg=el.tagName;
    if(tg==="INPUT"||tg==="SELECT"||tg==="TEXTAREA"||tg==="BUTTON"||tg==="A"||tg==="SCRIPT"||tg==="STYLE")continue;
    if(!isVisible(el))continue;
    var ot=ownText(el);
    if(!ot||ot.length>MAX_TEXT||!VALUE_RX.test(ot))continue;
    var selector=selectorFor(el);
    var key=selector+"|"+ot;
    if(seen[key])continue;seen[key]=1;
    out.push({label:clip(labelOf(el),MAX_LABEL),selector:clip(selector,200),text:clip(ot,MAX_TEXT)});
  }
  return out;
})()
"""


#: Bumped when the PII region snippet changes.
PII_REGIONS_JS_VERSION = "pii-regions-js-v1"

#: M3.1 / T-VIS-05 — WHERE ON THE PAGE A SCREENSHOT WOULD RENDER SOMETHING
#: SENSITIVE.  Returns ``{page_w, page_h, dpr, ok, regions:[{x,y,w,h,reason}]}``
#: in CSS pixels of the FULL PAGE (rect + scroll offset), which is the coordinate
#: space a ``full_page`` Playwright screenshot is captured in.
#:
#: The rule is SHAPE, NOT CONTENT.  Every control that can hold a typed value is
#: reported whether or not it currently holds one, because deciding per-field
#: would mean reading the value — and a value read in order to judge it is a
#: value one bug away from a log line.  Content matching is used only for text
#: the page RENDERS (an SSN printed on a review step is not in any input).
#:
#: ``ok:false`` is the honest failure: the caller must then refuse egress rather
#: than send an unmasked image, so a snippet that throws can never be mistaken
#: for a page with nothing to hide.
PII_REGIONS_JS = r"""
(function () {
  "use strict";
  var MAX = 400, out = [], ok = true;
  function push(el, reason) {
    if (out.length >= MAX) return;
    try {
      var r = el.getBoundingClientRect();
      if (!(r.width > 0 && r.height > 0)) return;
      var st = (el.ownerDocument.defaultView || window).getComputedStyle(el);
      if (st && (st.display === "none" || st.visibility === "hidden")) return;
      out.push({
        x: r.left + (window.scrollX || window.pageXOffset || 0),
        y: r.top + (window.scrollY || window.pageYOffset || 0),
        w: r.width, h: r.height, reason: reason
      });
    } catch (e) { ok = false; }
  }
  // ── 1. Controls that can hold a typed value ────────────────────────────────
  // A checkbox/radio/button/submit/reset/hidden holds no free text, so masking
  // one buys nothing and would blind the perceiver to a control it must see.
  var SKIP = {checkbox:1, radio:1, button:1, submit:1, reset:1, image:1, hidden:1, range:1, color:1};
  try {
    var ins = document.querySelectorAll("input, textarea, select, [contenteditable=''], [contenteditable='true']");
    for (var i = 0; i < ins.length; i++) {
      var el = ins[i], tag = (el.tagName || "").toLowerCase();
      if (tag === "input") {
        var t = ((el.getAttribute("type") || "text") + "").toLowerCase();
        if (SKIP[t]) continue;
      }
      push(el, tag === "select" ? "select_value" : "value_bearing_control");
    }
  } catch (e) { ok = false; }
  // ── 2. What the PAGE ITSELF declares sensitive ─────────────────────────────
  try {
    var declared = document.querySelectorAll(
      "[type='password'], [data-pii], [data-sensitive], " +
      "[autocomplete*='cc-'], [autocomplete*='name'], [autocomplete*='tel'], " +
      "[autocomplete*='email'], [autocomplete*='bday'], [autocomplete*='street'], " +
      "[autocomplete*='postal']");
    for (var d = 0; d < declared.length; d++) push(declared[d], "declared_sensitive");
  } catch (e) { ok = false; }
  // ── 3. RENDERED text that looks like an identifier ─────────────────────────
  // The review step of an application prints the SSN and the account number as
  // ordinary text; no input exists to mask. Own-text only, so masking a leaf
  // never blacks out a whole container.
  var RX = [
    /\b\d{3}-\d{2}-\d{4}\b/,                                   // SSN
    /\b(?:\d[ -]?){13,19}\b/,                                  // card / account
    /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/,      // email
    /\(?\b\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b/,                   // phone
    /\b\d{4}-\d{2}-\d{2}\b/,                                   // ISO date (DOB)
    /\b\d{1,2}\/\d{1,2}\/\d{4}\b/                              // US date (DOB)
  ];
  function ownText(el) {
    var t = "", ch = el.childNodes;
    for (var k = 0; k < ch.length; k++) if (ch[k].nodeType === 3) t += ch[k].nodeValue;
    return (t + "").replace(/\s+/g, " ").trim();
  }
  try {
    var all = document.body ? document.body.getElementsByTagName("*") : [];
    for (var j = 0; j < all.length && j < 6000 && out.length < MAX; j++) {
      var e2 = all[j], tg = (e2.tagName || "");
      if (tg === "SCRIPT" || tg === "STYLE" || tg === "NOSCRIPT") continue;
      var txt = ownText(e2);
      if (!txt || txt.length > 400) continue;
      for (var m = 0; m < RX.length; m++) {
        if (RX[m].test(txt)) { push(e2, "rendered_identifier"); break; }
      }
    }
  } catch (e) { ok = false; }
  var de = document.documentElement || {};
  var body = document.body || {};
  return {
    ok: ok && out.length < MAX,
    regions: out,
    page_w: Math.max(de.scrollWidth || 0, body.scrollWidth || 0,
                     de.clientWidth || 0, window.innerWidth || 0),
    page_h: Math.max(de.scrollHeight || 0, body.scrollHeight || 0,
                     de.clientHeight || 0, window.innerHeight || 0),
    dpr: window.devicePixelRatio || 1
  };
})()
"""
