"""FIELD AGENT (the second caged agent) — an LLM names a field, never fills it.

The exploration planner established the pattern: an LLM proposes, the deterministic
layer disposes.  This is the same cage around a different question.  The planner
ranks WHERE to go; this one says WHAT a field is for, so a value can be generated
for it by code.

WHAT THE MODEL SEES
    the field's SHAPE only — its label tokens, its input type, its own declared
    validation constraints, and the size bucket of its option list.  Never a value,
    never a URL, never a tenant, never a page.  A model that cannot see client data
    cannot leak, memorise or reproduce it, and that property is worth more than any
    accuracy the extra context would buy.

WHAT THE MODEL RETURNS
    a semantic TYPE from a closed vocabulary, and nothing else.  It does not
    return an e-mail address; it returns ``email``, and a deterministic generator
    produces the address from the crawl's fictional identity.  This is what makes
    every value reproducible from evidence and provably fictional — neither is
    possible if a model emits the digits.

A proposal can NEVER:
  * introduce a category (anything outside :data:`VOCABULARY` becomes ``unknown``);
  * override what the application itself declared (the deterministic classifier in
    the explorer consults ``autocomplete``, ``input_type`` and ``pattern`` FIRST,
    and only falls through to a learned type when the app said nothing);
  * cause a value to be typed into a field the generator refuses to answer (a
    one-time code stays residue no matter what the model calls it).

FAIL-OPEN BY CONSTRUCTION.  No LLM, a malformed answer, or nothing surviving
validation ⇒ an EMPTY map ⇒ the crawl behaves exactly as it did before.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable, Mapping

from ..clients import platform_api

logger = logging.getLogger(__name__)

#: Mirrored from the explorer's field_semantics. Duplicated rather than imported
#: because the services deploy independently: a shared import would let a
#: partial rollout produce types the other side cannot read.
#:
#: THERE ARE THREE COPIES, not two - here, platform-api's
#: services/test_factory/field_learning, and the explorer's field_semantics -
#: and each is pinned by its own test in its own process, because the two
#: services cannot share an interpreter. Adding "middle_name" to the explorer
#: alone failed platform-api's test_the_two_vocabularies_cannot_drift_apart;
#: fixing that pair alone then failed this one. A type that is not in ALL THREE
#: is written by one service and silently dropped by the next.
VOCABULARY = frozenset({
    "unknown", "given_name", "family_name", "full_name", "middle_name",
    "email", "phone",
    "date_of_birth", "age", "national_id", "street_address", "street_address_2",
    "city", "region", "postal_code", "country", "company", "job_title", "url",
    "card_number", "card_expiry", "card_cvc", "currency_amount", "percent",
    "quantity", "date", "time", "password", "one_time_code", "username",
    "free_text", "choice", "consent",
})

#: A plan is an optimiser, never an attack surface — the same bound the
#: exploration planner uses, for the same reason.
_MAX_FIELDS = 40
_SIGNATURE_RX = re.compile(r"^[a-f0-9]{8,64}$")

AGENT_SYSTEM = (
    "You classify web form fields by PURPOSE for automated software testing. You "
    "never produce a value of any kind - you only name what kind of value a field "
    "expects, choosing from a fixed list. Return STRICT JSON only."
)


def _describe(field: Mapping[str, Any]) -> dict[str, Any]:
    """The value-free description one field is presented as.

    Everything here is something the APPLICATION declared about the field. If a
    key could ever carry what a user typed, it does not belong in this function."""
    return {
        "id": str(field.get("signature") or "")[:64],
        "label_tokens": [str(t)[:40] for t in (field.get("tokens") or [])][:12],
        "input_type": str(field.get("input_type") or "")[:24],
        "constraints": str(field.get("constraints") or "")[:160],
        "option_count": str(field.get("option_shape") or "")[:12],
    }


def _build_prompt(fields: list[dict[str, Any]]) -> str:
    return (
        "Classify each web form field by what kind of value it expects.\n\n"
        "You are given only the field's declared shape - its label words, input "
        "type, validation constraints and how many options it offers. You are NOT "
        "given any values, and you must not produce any.\n\n"
        f"Allowed types (use EXACTLY one of these strings): {sorted(VOCABULARY)}\n\n"
        f"Fields: {json.dumps(fields)}\n\n"
        'Return STRICT JSON: {"fields": [{"id": <the id given>, "type": <one '
        'allowed type>, "confidence": 0.0-1.0}]}. Use "unknown" when the shape does '
        "not clearly indicate a purpose - a wrong confident answer is far worse "
        "than an honest unknown, because it causes the wrong value to be typed into "
        "a real application."
    )


def _strip_fences(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.lstrip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    return t.strip()


def _validate(raw_text: str, asked: set[str]) -> dict[str, dict]:
    """Parse, GROUND and bound the model's answer.

    Every rule fails CLOSED to dropping the entry: an answer that cannot be fully
    trusted simply classifies fewer fields, and an unclassified field is filled
    deterministically or asked about — both safe."""
    try:
        data = json.loads(_strip_fences(raw_text))
    except Exception:
        return {}
    proposed = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(proposed, list):
        return {}

    out: dict[str, dict] = {}
    for item in proposed:
        if not isinstance(item, Mapping) or len(out) >= _MAX_FIELDS:
            continue
        fid = str(item.get("id") or "").strip().lower()
        # GROUNDING: the model may only answer questions it was actually asked.
        # Without this it could invent a signature and have it stored as fact.
        if fid not in asked or not _SIGNATURE_RX.match(fid):
            if fid:
                logger.info("qec.field_agent.rejected_ungrounded id=%s", fid[:24])
            continue
        sem = str(item.get("type") or "").strip().lower().replace("-", "_").replace(" ", "_")
        if sem not in VOCABULARY or sem == "unknown":
            continue
        try:
            conf = float(item.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        # The model's own confidence is CAPPED below anything the application
        # declared about itself. A guess must never outrank a declaration.
        conf = max(0.4, min(0.7, conf))
        out[fid] = {"type": sem, "confidence": round(conf, 3), "source": "field_agent"}
    return out


async def classify_fields(tenant_id: str,
                          fields: Iterable[Mapping[str, Any]]) -> dict[str, dict]:
    """Name the fields nothing deterministic could name.

    Input is the crawl's residue: entries from the field ledger whose semantic type
    came back ``unknown``. Output is ``{signature: {type, confidence, source}}``,
    ready to be stored as a prior and shipped to the NEXT crawl of any application
    that has a field with the same signature.

    Never raises. An empty result is a correct, safe answer."""
    try:
        candidates = [_describe(f) for f in fields
                      if str((f or {}).get("signature") or "")]
        candidates = [c for c in candidates if c["id"] and c["label_tokens"]][:_MAX_FIELDS]
        if not candidates:
            return {}
        asked = {c["id"] for c in candidates}

        llm = await platform_api.complete_llm(
            tenant_id=tenant_id, prompt=_build_prompt(candidates),
            system=AGENT_SYSTEM, task="field_classification", max_tokens=1500,
        )
        if not llm.ok:
            logger.info("qec.field_agent.llm_unavailable detail=%s", (llm.detail or "")[:120])
            return {}
        verdicts = _validate(llm.text, asked)
        logger.info("qec.field_agent.classified asked=%d answered=%d",
                    len(candidates), len(verdicts))
        return verdicts
    except Exception as exc:                 # a classifier must never break a crawl
        logger.warning("qec.field_agent.failed error=%s", str(exc)[:200])
        return {}
