"""RUNG 3 — VALUES THE APPLICATION ITSELF SHOWED US.

WHY THIS EXISTS. No generator and no model can invent a customer that exists.
Point a crawl at a Sales Order and it needs a Customer the application will
accept; "Alex Morgan" is refused forever, by every application, however
plausibly it was written. That single fact is why business flows stall at their
first document, and it is not a data-generation problem — it is a data-SOURCE
problem.

The application answers it. A list page IS a table of valid values:

    {"Third-party name": "Book Keeping Company", "Customer Code": "CU1108-0004"}
    {"Ref.": "PR2001-0034", "Third-party": "Indian SAS", "Country": "India"}

MEASURED (Dolibarr, 2026-08-30) from its own third-party and proposal lists.
Those are referentially real: the record exists, so the form is accepted — and
nothing was invented, so the evidence stays as strong as the client's own data.

WHY NOT `displayed_values`. That capture already exists and was the obvious
candidate, but it pairs a value with whatever text sits near it — measured:
``{"label": "05/28/2022", "text": "60.00"}``, a date labelling an amount — and
it fires on 36 of 164 states. It was built to spot assertion CANDIDATES, not to
extract entities, and reusing it would have carried its mispairing into every
value we filled. A grid declares its own column headers; this reads those.

ENTITY-SHAPED, NOT A BAG OF STRINGS. One row is one entity, so a harvested
customer travels with ITS OWN city and code rather than another row's. A form
asking for a customer and its postcode gets both from the same record, which is
the difference between a form that validates and one that does not.

PRIVACY. The pool is in-process for the life of one crawl, never emitted, never
logged — the same contract as ``journey_values``, and for the same reason:
these are the client's own values, and they re-enter only the application they
came from.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

#: A table needs this many rows before it is a LIST OF RECORDS rather than a
#: form laid out in a table. Mirrors the grid rule the filter classifier uses.
MIN_GRID_ROWS = 3
#: Rows harvested per grid. A list of 500 customers is not 500 times more
#: useful than a list of 25, and the pool is held in memory.
MAX_ROWS_PER_GRID = 25
#: Entities kept per crawl, across all grids.
MAX_ENTITIES = 400

#: Reads a page's data grids into entities. Runs in the browser; returns
#: ``[{headers: [...], entities: [{column: cell}]}]``.
GRID_JS = r"""() => {
  const MIN_ROWS = %d, MAX_ROWS = %d, MAX_COLS = 20;
  const norm = s => (s == null ? "" : String(s)).replace(/\s+/g, " ").trim();
  const out = [];
  for (const table of document.querySelectorAll("table")) {
    const rows = [...table.querySelectorAll("tr")];
    if (rows.length < MIN_ROWS) continue;
    let headRow = rows.find(r => r.querySelector("th")) || rows[0];
    const headers = [...headRow.children].slice(0, MAX_COLS)
      .map(c => norm(c.textContent).replace(/[▲▼↑↓]/g, "").trim());
    if (!headers.filter(Boolean).length) continue;
    const entities = [];
    for (const r of rows) {
      if (r === headRow) continue;
      const cells = [...r.children];
      if (cells.length < 2) continue;
      // A DATA row carries no inputs of its own. A filter row does, and a row
      // checkbox is not an input for this purpose.
      if (r.querySelector("input:not([type=checkbox]), select, textarea")) continue;
      const ent = {};
      cells.slice(0, MAX_COLS).forEach((c, i) => {
        const h = headers[i], v = norm(c.textContent);
        if (h && v) ent[h] = v.slice(0, 120);
      });
      if (Object.keys(ent).length >= 2) entities.push(ent);
      if (entities.length >= MAX_ROWS) break;
    }
    if (entities.length) out.push({headers: headers.filter(Boolean), entities});
  }
  return out;
}""" % (MIN_GRID_ROWS, MAX_ROWS_PER_GRID)


def _norm(text: Any) -> str:
    return " ".join(("" if text is None else str(text)).split()).lower()


def _looks_like_a_value(text: str) -> bool:
    """Is this cell an ANSWER, or the application's own furniture?

    A grid cell holding "Edit", "—" or a bare count is chrome. Fail toward
    rejecting: a wrong harvested value is filled into a form, whereas a missed
    one merely falls through to the next rung.
    """
    t = (text or "").strip()
    if len(t) < 2 or len(t) > 120:
        return False
    if t in {"-", "—", "–", "n/a", "N/A"}:
        return False
    # Pure punctuation, or a lone digit-free symbol run.
    return bool(re.search(r"[A-Za-z0-9]", t))


class HarvestPool:
    """Entities the crawl has seen, keyed by the column that named them.

    In-process, per crawl. Never emitted, never logged — see the module note.
    """

    def __init__(self, *, max_entities: int = MAX_ENTITIES) -> None:
        self.max_entities = max_entities
        #: [{column_label: cell_text}] in first-seen order.
        self.entities: list[dict[str, str]] = []
        #: normalised column label -> [values], first-seen order, deduped.
        self._by_column: dict[str, list[str]] = {}

    # -- ingest ---------------------------------------------------------------
    def ingest(self, grids: Sequence[Mapping[str, Any]]) -> int:
        """Fold one page's grids into the pool. Returns entities added."""
        added = 0
        for grid in grids or ():
            for raw in (grid.get("entities") or ()):
                if len(self.entities) >= self.max_entities:
                    return added
                entity = {str(k): str(v) for k, v in dict(raw).items()
                          if str(k).strip() and _looks_like_a_value(str(v))}
                if not entity:
                    continue
                # TWO DIFFERENT QUESTIONS, kept apart on purpose.
                #
                # A column harvest asks "is this a valid value for this field?"
                # and one good cell answers it — a row whose only meaningful
                # cell is a company name still tells us that name exists.
                #
                # The ENTITY list answers "which values belong together?", and
                # a single cell cannot: relating a customer to its own postcode
                # needs at least two. Requiring two for BOTH (the first version
                # here) silently threw away every value on a sparse row.
                for column, value in entity.items():
                    bucket = self._by_column.setdefault(_norm(column), [])
                    if value not in bucket:
                        bucket.append(value)
                if len(entity) < 2:
                    continue
                self.entities.append(entity)
                added += 1
        return added

    # -- read -----------------------------------------------------------------
    def candidates(self, label: str) -> list[str]:
        """Values seen under a column that matches ``label``.

        STRICT, and deliberately so: exact match on the normalised label, then a
        whole-phrase containment either way. A loose match here would type a
        customer code into a postcode — the same collision the client data
        library refuses by design, for the same reason.
        """
        key = _norm(label)
        if not key:
            return []
        exact = self._by_column.get(key)
        if exact:
            return list(exact)
        for column, values in self._by_column.items():
            if len(column) >= 4 and (column in key or key in column):
                return list(values)
        return []

    def value_for(self, label: str, *, refused: Sequence[str] = ()) -> Optional[str]:
        """One harvested value for a field, or None."""
        blocked = {str(r) for r in refused}
        for value in self.candidates(label):
            if value not in blocked:
                return value
        return None

    def stats(self) -> dict[str, Any]:
        return {"entities": len(self.entities), "columns": len(self._by_column)}
