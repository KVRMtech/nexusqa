"""UI dictionary registry — signature computation, lookup, and upsert.

Operates on a tenant-scoped library of UI controls keyed by
``element_signature`` — a deterministic uuid5 over (page_key,
normalised element_type, normalised label_text).  Two captures of the
same control on the same page collide on this signature so an upsert
always finds the existing row and bumps its recognition_count.

Design principles:

  * Pure transformation in :func:`compute_element_signature` — no DB,
    no async, fully testable in isolation.
  * Bulk operations: :meth:`UIDictionary.lookup_signatures` and
    :meth:`UIDictionary.upsert_entries` are both batched so a scene
    with 30 controls touches the DB twice, not 60 times.
  * Confidence math is encapsulated: callers ask the dictionary to
    record a recognition (with the freshly-extracted selector and
    confidence); the dictionary decides how to merge with the prior
    entry.  Selector replacement is conservative — a new selector
    only replaces the stored one when it is at least as confident,
    avoiding regressions when a noisy frame produces a worse extraction.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import UIDictionaryEntryRow


_NS_DICTIONARY = uuid.UUID("8b1f3a64-9a44-4ec1-9d77-3f7b2c5a4e10")  # stable namespace


# ─── Signature ───────────────────────────────────────────────────────────────

_LABEL_NOISE_RE = re.compile(r"[\s ]+")


def _normalize_label(value: str) -> str:
    """Lower-case, collapse whitespace, strip punctuation tail noise.

    The signature must be insensitive to incidental OCR variation so two
    captures of the same control match.  At the same time we keep
    distinguishing characters (digits, hyphens) so two genuinely-different
    fields don't collide (e.g. "ZIP" vs "ZIP+4").
    """
    if value is None:
        return ""
    s = str(value).lower().strip()
    s = _LABEL_NOISE_RE.sub(" ", s)
    s = s.strip(" .,:;!?\"'`")
    return s


def _normalize_element_type(value: str) -> str:
    if value is None:
        return ""
    return str(value).lower().strip().replace("-", "_")


def _normalize_page_key(value: str) -> str:
    if value is None:
        return ""
    return str(value).lower().strip().rstrip(".")


def compute_element_signature(
    *,
    page_key: str,
    element_type: str,
    label_text: str,
) -> str:
    """Deterministic identity for a UI control.

    Returns a 32-char hex string (32-char head of the uuid5 hex
    representation).  Two controls produce the same signature when
    their (page_key, element_type, label_text) match after
    normalisation.

    The signature is intentionally narrow — bounding box geometry is
    NOT part of identity because the same button can shift a few px
    across viewport sizes without changing semantically.  For pages
    without a stable page_key (no URL) we fall back to the OCR-derived
    page title, accepting that the dictionary is less effective there.
    """
    page = _normalize_page_key(page_key)
    et = _normalize_element_type(element_type)
    label = _normalize_label(label_text)
    payload = f"{page}|{et}|{label}"
    return uuid.uuid5(_NS_DICTIONARY, payload).hex


# ─── Public lookup / upsert API ──────────────────────────────────────────────

@dataclass
class DictionaryRecognition:
    """One control extraction the orchestrator wants to record.

    The dictionary computes the signature internally; callers only
    supply the natural fields they have.  When ``preferred_selector``
    is empty or vision-only, the dictionary keeps any stronger prior
    selector unchanged.
    """

    page_key: str
    domain: str
    element_type: str
    label_text: str
    display_label: str = ""
    action_kind: str = ""
    preferred_selector: str = ""
    selector_confidence: float = 0.0
    selector_source: str = "unknown"
    bbox_centre_x: Optional[int] = None
    bbox_centre_y: Optional[int] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def signature(self) -> str:
        return compute_element_signature(
            page_key=self.page_key,
            element_type=self.element_type,
            label_text=self.label_text,
        )


@dataclass
class DictionaryHit:
    """A pre-existing dictionary row matched against a recognition."""

    entry_id: str
    signature: str
    preferred_selector: str
    selector_confidence: float
    selector_source: str
    action_kind: str
    recognition_count: int
    automation_success_count: int
    automation_failure_count: int
    bbox_centre_x: Optional[int]
    bbox_centre_y: Optional[int]


class UIDictionary:
    """Tenant-scoped UI dictionary client.

    Construct once per request with an active session; instances are
    stateless apart from the bound tenant_id.
    """

    def __init__(self, session: AsyncSession, tenant_id: str):
        if not tenant_id:
            raise ValueError("UIDictionary requires a tenant_id")
        self._session = session
        self._tenant_id = tenant_id

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    # ── Lookup ──────────────────────────────────────────────────

    async def lookup_signatures(
        self, signatures: Iterable[str],
    ) -> dict[str, DictionaryHit]:
        """Return existing entries for the supplied signatures.

        The result maps signature → :class:`DictionaryHit`.  Signatures
        that do not exist are simply absent from the map (callers treat
        missing keys as "new control, register it").
        """
        sig_list = [s for s in signatures if s]
        if not sig_list:
            return {}

        rows = await self._session.execute(
            select(UIDictionaryEntryRow).where(
                UIDictionaryEntryRow.tenant_id == self._tenant_id,
                UIDictionaryEntryRow.element_signature.in_(sig_list),
            )
        )
        out: dict[str, DictionaryHit] = {}
        for row in rows.scalars().all():
            out[row.element_signature] = DictionaryHit(
                entry_id=row.entry_id,
                signature=row.element_signature,
                preferred_selector=row.preferred_selector or "",
                selector_confidence=float(row.selector_confidence or 0.0),
                selector_source=row.selector_source or "unknown",
                action_kind=row.action_kind or "",
                recognition_count=int(row.recognition_count or 0),
                automation_success_count=int(row.automation_success_count or 0),
                automation_failure_count=int(row.automation_failure_count or 0),
                bbox_centre_x=row.bbox_centre_x,
                bbox_centre_y=row.bbox_centre_y,
            )
        return out

    # ── Recognition recording ────────────────────────────────────

    async def record_recognitions(
        self, recognitions: Iterable[DictionaryRecognition],
    ) -> dict[str, DictionaryHit]:
        """Insert-or-update one row per recognition.

        Selector replacement is **monotonic non-decreasing**: a fresh
        selector only displaces the stored one when its confidence is
        strictly greater (or equal-with-richer-source).  This protects
        against a noisy frame in the middle of an artifact dragging
        the dictionary's quality down.

        Returns the post-upsert :class:`DictionaryHit` map (signature →
        latest hit) so the caller can fold the dictionary's verdict
        back into its evidence_step rows immediately.
        """
        recs = list(recognitions)
        if not recs:
            return {}

        # Step 1 — load any existing rows so we can compute monotonic
        # replacement and choose between INSERT and UPDATE branches in
        # a single pass.
        signatures = [r.signature for r in recs]
        existing = await self.lookup_signatures(signatures)

        # Step 2 — build the bulk-insert payload and the per-row
        # ON CONFLICT DO UPDATE expression.  Using the postgres
        # dialect's pg_insert lets us reference EXCLUDED columns in
        # the SET clause so the merge math runs in-database.
        rows_to_upsert: list[dict] = []
        post_state: dict[str, DictionaryHit] = {}

        for rec in recs:
            prior = existing.get(rec.signature)
            replace_selector = _should_replace_selector(prior, rec)

            new_sel = (
                rec.preferred_selector
                if replace_selector
                else (prior.preferred_selector if prior else rec.preferred_selector)
            )
            new_conf = (
                rec.selector_confidence
                if replace_selector
                else (prior.selector_confidence if prior else rec.selector_confidence)
            )
            new_source = (
                rec.selector_source
                if replace_selector
                else (prior.selector_source if prior else rec.selector_source)
            )
            new_action_kind = rec.action_kind or (prior.action_kind if prior else "")

            new_recognition_count = (prior.recognition_count if prior else 0) + 1
            entry_id = prior.entry_id if prior else str(uuid.uuid4())

            rows_to_upsert.append({
                "entry_id": entry_id,
                "tenant_id": self._tenant_id,
                "element_signature": rec.signature,
                "page_key": _normalize_page_key(rec.page_key)[:200],
                "domain": (rec.domain or "")[:200],
                "element_type": _normalize_element_type(rec.element_type)[:50],
                "label_text": (rec.label_text or "")[:500],
                "display_label": (rec.display_label or "")[:600],
                "action_kind": new_action_kind[:32],
                "preferred_selector": (new_sel or "")[:2000],
                "selector_confidence": float(new_conf),
                "selector_source": (new_source or "unknown")[:20],
                "recognition_count": new_recognition_count,
                "bbox_centre_x": rec.bbox_centre_x,
                "bbox_centre_y": rec.bbox_centre_y,
                "metadata_json": dict(rec.metadata or {}),
            })

            post_state[rec.signature] = DictionaryHit(
                entry_id=entry_id,
                signature=rec.signature,
                preferred_selector=new_sel or "",
                selector_confidence=float(new_conf),
                selector_source=new_source or "unknown",
                action_kind=new_action_kind,
                recognition_count=new_recognition_count,
                automation_success_count=(prior.automation_success_count if prior else 0),
                automation_failure_count=(prior.automation_failure_count if prior else 0),
                bbox_centre_x=rec.bbox_centre_x,
                bbox_centre_y=rec.bbox_centre_y,
            )

        from sqlalchemy import func as sa_func
        stmt = pg_insert(UIDictionaryEntryRow).values(rows_to_upsert)
        upsert = stmt.on_conflict_do_update(
            constraint="uq_ui_dictionary_tenant_signature",
            set_={
                "page_key": stmt.excluded.page_key,
                "domain": stmt.excluded.domain,
                "element_type": stmt.excluded.element_type,
                "label_text": stmt.excluded.label_text,
                "display_label": stmt.excluded.display_label,
                "action_kind": stmt.excluded.action_kind,
                "preferred_selector": stmt.excluded.preferred_selector,
                "selector_confidence": stmt.excluded.selector_confidence,
                "selector_source": stmt.excluded.selector_source,
                "recognition_count": stmt.excluded.recognition_count,
                "bbox_centre_x": stmt.excluded.bbox_centre_x,
                "bbox_centre_y": stmt.excluded.bbox_centre_y,
                "metadata_json": stmt.excluded.metadata_json,
                "last_seen_at": sa_func.now(),
            },
        )
        await self._session.execute(upsert)
        return post_state

    # ── Automation feedback (closed-loop) ────────────────────────

    async def record_automation_outcome(
        self, *, signature: str, success: bool,
    ) -> None:
        """Bump success/failure counters after a Playwright run.

        Used by the closed-loop test executor when it lands.  The
        dictionary tolerates the signature being absent (no-op) so the
        runner does not have to maintain a separate sync mechanism.
        """
        from sqlalchemy import update
        stmt = (
            update(UIDictionaryEntryRow)
            .where(
                UIDictionaryEntryRow.tenant_id == self._tenant_id,
                UIDictionaryEntryRow.element_signature == signature,
            )
            .values(
                automation_success_count=(
                    UIDictionaryEntryRow.automation_success_count + (1 if success else 0)
                ),
                automation_failure_count=(
                    UIDictionaryEntryRow.automation_failure_count + (0 if success else 1)
                ),
            )
        )
        await self._session.execute(stmt)


# ─── Internal helpers ────────────────────────────────────────────────────────

def _should_replace_selector(
    prior: Optional[DictionaryHit], rec: DictionaryRecognition,
) -> bool:
    """Decide whether a fresh selector is strictly better than the prior one.

    Three rules, applied in order:

      1. No prior → always take the new one (initial population).
      2. New selector is empty → keep the prior (do not regress to nothing).
      3. New confidence > prior confidence → take the new one.
      4. New confidence == prior confidence AND new source is richer
         (ocr > hybrid > vision > unknown) → take the new one.

    Otherwise keep the prior.  This monotonic non-decreasing rule
    protects the dictionary against single-frame noise.
    """
    if prior is None:
        return True
    if not rec.preferred_selector:
        return False
    if rec.selector_confidence > prior.selector_confidence:
        return True
    if rec.selector_confidence == prior.selector_confidence:
        rank = {"ocr": 3, "hybrid": 2, "vision": 1, "unknown": 0}
        if rank.get(rec.selector_source, 0) > rank.get(prior.selector_source, 0):
            return True
    return False
