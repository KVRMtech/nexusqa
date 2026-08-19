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
INVENTORY_JS_VERSION = "inv-js-v10"

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

INVENTORY_JS = r"""
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

  // ---- iframe selector (mirrors compiler.py:1005-1011) ---------------------

  // An IDENTIFIER in selector position — an id following '#'. CSS.escape is the
  // standard answer, and the label[for] lookup above already uses it; this
  // recipe simply never did. Unescaped, id="pay.frame" yields `iframe#pay.frame`,
  // which is VALID CSS that means something else entirely ("id=pay AND
  // class=frame") — so it fails silently, matching nothing, while the manifest
  // records every control in that frame as captured.
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

  function frameSelectorFor(iframeEl, index) {
    try {
      if (iframeEl.id) return 'iframe#' + cssIdent(iframeEl.id);
      var nm = attr(iframeEl, "name");
      if (nm) return 'iframe[name="' + cssStr(nm) + '"]';
      var title = attr(iframeEl, "title");
      if (title) return 'iframe[title="' + cssStr(title) + '"]';
      var src = attr(iframeEl, "src");
      if (src) return 'iframe[src="' + cssStr(src) + '"]';
    } catch (e) {}
    return "iframe >> nth=" + index;
  }

  // ---- collection ----------------------------------------------------------

  function describe(el, doc, frameSelector) {
    var role = implicitRole(el);
    var an = accessibleName(el, doc);
    var tag = lc(el.tagName);
    var type = lc(el.type || "");
    var best = an.source === "title" || an.source === "placeholder";
    var opt = optionsAndTotalOf(el);
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
      required: isRequired(el),
      disabled: isDisabled(el),
      frame_selector: frameSelector || "",
      testid: testId(el),
      css_hint: cssHint(el),
      value_committed: valueCommitted(el),
      href: hrefOf(el),
      haspopup: lc(attr(el, "aria-haspopup")),
      expanded: lc(attr(el, "aria-expanded")),
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
      landmark: nearestLandmark(el, doc)
    };
  }

  // Walk a document/shadow-root subtree; recurse open shadow roots + iframes.
  function walk(root, doc, frameSelector, sink, seenDocs) {
    var nodes;
    try { nodes = root.querySelectorAll(SELECTOR); } catch (e) { return; }
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!isVisible(el)) continue;
      try { sink.push(describe(el, doc, frameSelector)); } catch (e) {}
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
    var all;
    try { all = root.querySelectorAll("*"); } catch (e) { all = []; }
    for (var j = 0; j < all.length; j++) {
      var host = all[j];
      if (host.shadowRoot) {
        walk(host.shadowRoot, host.shadowRoot, frameSelector, sink, seenDocs);
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
        try { walk(cdoc, cdoc, sel, sink, seenDocs); } catch (e) {}
      }
    }
  }

  var out = [];
  var seen = [document];
  walk(document, document, "", out, seen);
  return out;
})()
""".replace("__MAX_OPTIONS__", str(MAX_OPTIONS))


#: OPAQUE-SURFACE detector — positively FINDS the surfaces the DOM walker cannot read, so a
#: blind spot becomes a named ledger row instead of an empty "clean" scan. Detects (a)
#: cross-origin iframes (Stripe/reCAPTCHA/maps/Plaid), (b) large canvas-rendered UIs
#: (Flutter/WebGL/charts), (c) custom elements rendering via a CLOSED shadow root (heuristic:
#: a dash-tagged element with size but no readable light DOM). Labels/kinds only, never a
#: fabricated capture — the honest anti-green-wash of coverage.
OPAQUE_JS = r"""
(function () {
  var out = [], MAXO = 40, seen = {};
  function vis(el){ try { var r = el.getBoundingClientRect(); var s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.display !== "none" && s.visibility !== "hidden"
      && parseFloat(s.opacity || "1") > 0; } catch (e) { return false; } }
  function push(kind, label, reason){
    var key = kind + "|" + label;
    if (out.length < MAXO && !seen[key]) { seen[key] = 1;
      out.push({ kind: kind, label: ("" + label).slice(0, 160), reason: reason }); } }
  try {
    var frames = document.querySelectorAll("iframe");
    for (var i = 0; i < frames.length; i++) { var f = frames[i]; if (!vis(f)) continue;
      var readable = false; try { readable = !!f.contentDocument; } catch (e) { readable = false; }
      if (!readable) { var src = ""; try { src = f.getAttribute("src") || ""; } catch (e) {}
        var host = src; try { host = new URL(src, location.href).host; } catch (e) {}
        push("cross_origin_iframe", host || "embedded frame",
             "a cross-origin embed the DOM can't read (e.g. payment/captcha/map)"); } }
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
      if (el.childElementCount > 0) continue;          // has light DOM we read
      if ((el.textContent || "").trim()) continue;     // has readable text
      if (!vis(el)) continue;
      var r = el.getBoundingClientRect(); if (r.height < 40) continue;
      push("closed_shadow", tag,
           "a <" + tag + "> element rendering via a closed shadow root the DOM can't pierce"); }
  } catch (e) {}
  return out;
})()
"""

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
