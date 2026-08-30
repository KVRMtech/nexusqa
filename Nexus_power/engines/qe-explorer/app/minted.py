"""RUNG 4 — WHAT THE APPLICATION MINTED, SO ONE FLOW CAN FEED THE NEXT.

THE PROBLEM, WHICH NO OTHER RUNG CAN SOLVE. A service or claims flow opens by
demanding something that does not exist until an earlier flow created it:

    "Policy Number"        exists only after an application was issued
    "Claim Reference"      exists only after a claim was filed
    "Quote ID"             exists only after a quote was run
    "Application Number"   exists only after an application was submitted

No generator can invent one — the application checks its own database. No model
can know one. The client cannot list them in advance, because they will not
exist until the crawl runs. Harvest (rung 3) finds them ONLY if some list page
happens to display them, which service portals routinely do not.

But the crawl itself just created one. It walked the apply funnel, the
application was submitted, and the confirmation screen printed the number. That
number is the key to every downstream flow, and throwing it away is why crawls
cover the front of a product and nothing behind it.

WHY THIS SITS ABOVE HARVEST. A harvested value was displayed by the application
but may be stale, already consumed, or belong to somebody else's test run. A
MINTED value was created by THIS crawl, minutes ago, and is therefore live by
construction — the strongest evidence in the ladder short of the client stating
it outright.

MINTED IS DEFINED BY ACT-THEN-DIFF, NOT BY LOOKING LIKE AN ID. The distinction
between rung 3 and rung 4 is not the shape of the value; it is causation. A
reference is minted only when it was ABSENT before the crawl acted and PRESENT
after — which is exactly what proves the application created it in response.
Recording every id-shaped string on a confirmation page instead would sweep up
the customer's own account number, the page's build id and yesterday's data,
and hand a downstream flow a reference the crawl did not actually create.

The registry is in-process and per crawl. Values never reach evidence — the
counts do. A minted reference is real client-system data, so it is treated with
the same care as a harvested one.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional, Sequence

#: A registry that grows without bound would hold a whole crawl's text.
MAX_REFERENCES = 200

#: A reference shorter than this is a table's row-count, not an identifier.
MIN_REFERENCE_CHARS = 4
MAX_REFERENCE_CHARS = 64

#: Words that make a LABEL a reference-bearing one. The label carries most of
#: the evidence: "Application Number" names a minted reference, "Annual Income"
#: does not, and both hold digits.
_REFERENCE_WORDS = (
    "number", "no", "num", "id", "identifier", "reference", "ref", "code",
    "confirmation", "receipt", "policy", "claim", "case", "ticket", "order",
    "quote", "application", "account", "member", "certificate", "transaction",
    "tracking", "invoice", "contract",
)
_REFERENCE_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _REFERENCE_WORDS) + r")\b")

#: Things that are id-SHAPED but are never a minted reference. Each was chosen
#: because it appears on real confirmation screens beside the real one.
_NOT_A_REFERENCE = (
    re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$"),      # a date
    re.compile(r"^\d{1,2}:\d{2}"),                        # a time
    re.compile(r"^[$£€]"),                                # money
    re.compile(r"^\d+(\.\d+)?\s*%$"),                     # a percentage
)

#: Reading the page for "label: value" pairs. Confirmation surfaces render them
#: as definition lists, table rows, or a labelled span — all three here, because
#: picking one would work on a third of real applications.
MINTED_JS = r"""() => {
  const out = [];
  const text = (el) => (el && el.textContent || '').replace(/\s+/g, ' ').trim();
  const push = (label, value) => {
    if (label && value && label !== value) out.push({label, value});
  };

  // 1. definition lists: <dt>Policy Number</dt><dd>P-1001</dd>
  for (const dl of document.querySelectorAll('dl')) {
    const kids = [...dl.children];
    for (let i = 0; i < kids.length - 1; i++) {
      if (kids[i].tagName === 'DT' && kids[i + 1].tagName === 'DD') {
        push(text(kids[i]), text(kids[i + 1]));
      }
    }
  }

  // 2. two-cell table rows: <tr><th>Claim Ref</th><td>C-77</td></tr>
  for (const tr of document.querySelectorAll('tr')) {
    const cells = [...tr.children].filter(c => /^(TD|TH)$/.test(c.tagName));
    if (cells.length === 2) push(text(cells[0]), text(cells[1]));
  }

  // 3. a labelled element followed by its value, and "Label: VALUE" in one
  //    node — the shape most hand-written confirmation panels actually use.
  for (const el of document.querySelectorAll(
         'p, li, div, span, td, h1, h2, h3, h4, strong, b')) {
    if (el.children.length > 2) continue;          // a container, not a line
    const whole = text(el);
    if (!whole || whole.length > 160) continue;
    const m = whole.match(/^(.{2,60}?)\s*[:#]\s*(.+)$/);
    if (m) push(m[1], m[2]);
  }
  return out;
}"""


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def is_reference_label(label: str) -> bool:
    """Does this label name something an application MINTS?

    Fails toward rejecting. A missed reference falls to the next rung; a wrong
    one is typed into a downstream form and dead-ends the flow it was meant to
    open.
    """
    key = _norm(label).rstrip(":#").strip()
    if not key or len(key) > 60:
        return False
    return bool(_REFERENCE_RE.search(key))


def looks_like_a_reference(value: str) -> bool:
    """Is this VALUE plausibly a minted identifier?

    Deliberately weak on purpose — the LABEL carries the evidence. This only
    rejects the shapes that sit next to a real reference on a confirmation
    screen and would otherwise be mistaken for one.
    """
    text = (value or "").strip()
    if not (MIN_REFERENCE_CHARS <= len(text) <= MAX_REFERENCE_CHARS):
        return False
    if not re.search(r"\d", text):
        # A reference without a digit is almost always prose ("Approved",
        # "Thank you for your application").
        return False
    if " " in text and len(text.split()) > 4:
        return False                                   # a sentence, not an id
    for pattern in _NOT_A_REFERENCE:
        if pattern.match(text):
            return False
    return True


class MintRegistry:
    """References THIS crawl caused the application to create.

    In-process, per crawl. Never emitted and never logged — see the module note.
    """

    def __init__(self, *, max_references: int = MAX_REFERENCES) -> None:
        self.max_references = max_references
        #: normalised label -> value, first-seen order (a dict preserves it).
        self._by_label: dict[str, str] = {}
        #: Every value the registry has ever been offered, minted or not. This
        #: is the "before" side of act-then-diff: a value already on the page
        #: before the crawl acted cannot have been minted BY the crawl.
        self._seen_values: set[str] = set()

    # -- the "before" half of act-then-diff -----------------------------------
    def observe(self, pairs: Iterable[Mapping[str, Any]]) -> None:
        """Record what a page showed WITHOUT crediting the crawl for it.

        Called on every page. What it captures is the baseline: anything already
        visible here was not created by an action that has not happened yet.
        """
        for pair in pairs or ():
            value = str((pair or {}).get("value") or "").strip()
            if value:
                self._seen_values.add(value)

    # -- the "after" half, which is the only path that mints ------------------
    def mint(self, pairs: Iterable[Mapping[str, Any]]) -> list[str]:
        """Fold a POST-ACTION page in, keeping only what the action created.

        Returns the labels newly minted, so a caller can record a count without
        touching a value. Call this ONLY after the crawl acted — calling it on
        an ordinary page would credit the application's existing data to the
        crawl and defeat the distinction this rung is built on.
        """
        minted: list[str] = []
        for pair in pairs or ():
            if len(self._by_label) >= self.max_references:
                break
            label = str((pair or {}).get("label") or "")
            value = str((pair or {}).get("value") or "").strip()
            if not value:
                continue
            # THE DIFF. A value visible before the action was not minted by it,
            # whatever its label says. Recorded as seen either way so a second
            # sighting cannot later be mistaken for a fresh mint.
            was_new = value not in self._seen_values
            self._seen_values.add(value)
            if not was_new:
                continue
            if not is_reference_label(label) or not looks_like_a_reference(value):
                continue
            key = _norm(label).rstrip(":#").strip()
            if key and key not in self._by_label:
                self._by_label[key] = value
                minted.append(key)
        return minted

    # -- read -----------------------------------------------------------------
    def value_for(self, label: str, *, refused: Sequence[str] = ()) -> Optional[str]:
        """The minted reference for a field, or None.

        STRICT match, for harvest's reason: exact on the normalised label, then
        whole-phrase containment either way. A loose match would type a policy
        number into a claim-reference field, and the flow it was meant to open
        would dead-end on a validation the application was right to raise.
        """
        key = _norm(label).rstrip(":#").strip()
        if not key:
            return None
        blocked = {str(r) for r in refused}

        candidates: list[str] = []
        exact = self._by_label.get(key)
        if exact is not None:
            candidates.append(exact)
        else:
            for stored, value in self._by_label.items():
                if len(stored) >= 4 and (stored in key or key in stored):
                    candidates.append(value)
        for value in candidates:
            if value not in blocked:
                return value
        return None

    # -- what evidence is allowed to know -------------------------------------
    @property
    def count(self) -> int:
        """How many references this crawl minted. A COUNT, never a value."""
        return len(self._by_label)

    def labels(self) -> list[str]:
        """The LABELS minted, for evidence. Labels are the application's own
        wording — the question, never the answer — which is the same line the
        rest of the evidence pipeline holds."""
        return list(self._by_label)
