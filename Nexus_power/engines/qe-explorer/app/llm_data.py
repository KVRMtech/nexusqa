"""THE LLM DATA AGENT — rung 8 of the value ladder (opt-in, provenance-stamped).

WHY THIS EXISTS. The operator can choose, in the portal, that a crawl must never
stop for data: every field gets the most plausible value the system can produce,
and the evidence records where each value came from. The deterministic ladder
already answers most classes; what it cannot answer it hands to this agent
before anything is allowed to become residue — free text that fell back to the
literal "autotest" (39 of 48 measured live rejections), fields no rung could
classify, and repair, where the application has just named its own rule.

THE WIRE IS QE-CENTRAL, NOT A PROVIDER. The first version of this module built
its own ``httpx.Client`` against ``api.openai.com`` — exactly the "second
route" qe-central's T-SEC-12 exists to forbid ("a caller that wants to skip
[the PII egress guard] would have to write its own HTTP client"). That test
could not see a client living in this service, so nothing went red while field
labels and section headings egressed unscanned. The default transport is now
qe-central's ``/internal/pick-value`` — HMAC-signed like ``/pick-advance``,
scanned at the one guarded wire (``platform_api.complete_llm``) — and the
explorer's own suite pins that this file is the only module here that may even
name a model host, and only behind the dev flag below.

DIRECT MODE IS FOR A DEVELOPER'S BENCH, NOT A DEPLOYMENT. With
``QEC_DATA_LLM_DIRECT=true`` (and an ``OPENAI_API_KEY``) the agent calls the
provider itself — useful against a local fixture with no qe-central running.
Without that explicit flag the direct path refuses without sending anything.

WHAT IT MUST NEVER DO, and the tests hold it to:

  * override a truer rung — the client's answer key, journey memory, a recalled
    value always win; the agent only fills what would otherwise be empty;
  * answer a CREDENTIAL — a password or one-time code is not data;
  * return an option the control does not offer — replies are clamped to the
    control's own labels, case-insensitively, or become None (the server clamps
    too; both sides on purpose);
  * stop the crawl — every failure path (no config, HTTP error, timeout, the
    per-crawl cap, the open breaker, an egress refusal upstream) returns None,
    which is exactly the residue behaviour the crawl has always had.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional, Sequence

import httpx

from .hmac_auth import tenant_scope

logger = logging.getLogger(__name__)

MODE_CENTRAL = "central"
MODE_DIRECT = "direct"

#: Semantic types that are credentials, not data. The agent refuses these
#: unconditionally — a fabricated OTP is an auth attempt, not an answer.
_CREDENTIAL_TYPES = frozenset({"password", "otp", "one_time_code"})

_SYSTEM_PROMPT = (
    "You supply ONE test value for ONE form field in a disposable test "
    "environment. Reply with the value only — no quotes, no explanation, no "
    "labels. Rules: "
    "(1) If a list of allowed options is given, reply with EXACTLY one of them, "
    "verbatim. "
    "(2) Respect every stated constraint (pattern, min, max, maxlength, date "
    "windows). "
    "(3) For government or financial identifiers use designated TEST ranges "
    "only: SSNs in the 900-series, card numbers that pass Luhn from published "
    "test prefixes (e.g. 4111111111111111), routing number 021000021. "
    "(4) Prefer a plausible, internally consistent value over a clever one; "
    "short over long. "
    "(5) If a rejection message from the application is provided, your value "
    "MUST satisfy the rule that message states. "
    "(6) Never reply with a refusal or a question — always produce a value."
)


def _clamp(value: str, options: Sequence[str]) -> Optional[str]:
    """The control's own label for an on-list reply, else None (enumerables)."""
    cleaned = (value or "").strip().strip('"').strip("'").split("\n")[0][:500]
    if not cleaned:
        return None
    if not options:
        return cleaned
    for option in options:
        if cleaned.lower() == str(option).strip().lower():
            return str(option)
    return None


class LLMDataAgent:
    """Sync value provider. Built once per crawl; never raises; None on failure."""

    def __init__(self, *, mode: str = MODE_CENTRAL, settings: Any = None,
                 crawl_id: str = "", tenant_id: str = "",
                 model: str = "", max_calls: int = 150,
                 breaker_threshold: int = 3, timeout_s: float = 10.0,
                 transport: Optional[httpx.BaseTransport] = None) -> None:
        self.mode = mode if mode in (MODE_CENTRAL, MODE_DIRECT) else MODE_CENTRAL
        self.settings = settings
        self.crawl_id = crawl_id
        #: The tenant this crawl belongs to. Part of the SIGNING SCOPE for
        #: /internal/pick-value since the tenant-scoped HMAC migration —
        #: see _ask_central. Defaulted so every existing test double that
        #: constructs this agent without one keeps working.
        self.tenant_id = tenant_id
        self.model = model or os.environ.get("QEC_DATA_LLM_MODEL", "gpt-4o-mini")
        self.max_calls = max_calls
        self.breaker_threshold = breaker_threshold
        self.calls = 0
        self.answered = 0
        self.failures_in_a_row = 0
        self.breaker_open = False
        self._client = httpx.Client(timeout=timeout_s, transport=transport)

    # -- the one entry point --------------------------------------------------
    def value_for(self, *, name: str, semantic_type: str, kind: str,
                  options: Sequence[str] = (), constraints: str = "",
                  section: str = "", page_title: str = "",
                  rejection: str = "") -> Optional[str]:
        if semantic_type in _CREDENTIAL_TYPES or kind == "password":
            return None
        if self.breaker_open or self.calls >= self.max_calls:
            if self.calls == self.max_calls and not self.breaker_open:
                logger.info("qec.llm_data.cap_reached max_calls=%d", self.max_calls)
            return None
        try:
            if self.mode == MODE_DIRECT:
                raw = self._ask_direct(
                    name=name, semantic_type=semantic_type, kind=kind,
                    options=options, constraints=constraints, section=section,
                    page_title=page_title, rejection=rejection)
            else:
                raw = self._ask_central(
                    name=name, semantic_type=semantic_type, kind=kind,
                    options=options, constraints=constraints, section=section,
                    page_title=page_title, rejection=rejection)
        except Exception as exc:                                   # noqa: BLE001
            self._note_failure(exc)
            return None
        self.failures_in_a_row = 0
        if raw is None:
            return None
        value = _clamp(raw, options)
        if value is None:
            logger.info("qec.llm_data.off_list field=%r got=%r",
                        str(name)[:40], str(raw)[:40])
            return None
        self.answered += 1
        return value

    # -- central: the guarded wire (default) ----------------------------------
    def _ask_central(self, **field_ctx: Any) -> Optional[str]:
        """POST /internal/pick-value on qe-central, HMAC-signed like pick-advance.

        The server owns the model call, the PII egress scan and the spend
        accounting; ``none``/``unavailable`` both come back as an honest None.
        """
        settings = self.settings
        if settings is None:
            logger.info("qec.llm_data.unavailable error=no_settings_for_central")
            return None
        body = {
            "crawl_id": self.crawl_id,
            # BOTH identities travel, because both are inside the signature
            # scope on the far side (internal._authenticate_internal).
            "tenant_id": self.tenant_id,
            "name": str(field_ctx.get("name") or ""),
            "semantic_type": str(field_ctx.get("semantic_type") or ""),
            "kind": str(field_ctx.get("kind") or ""),
            "options": [str(o) for o in (field_ctx.get("options") or ())][:60],
            "constraints": str(field_ctx.get("constraints") or "")[:400],
            "section": str(field_ctx.get("section") or "")[:200],
            "page_title": str(field_ctx.get("page_title") or "")[:200],
            "rejection": str(field_ctx.get("rejection") or "")[:400],
        }
        payload = json.dumps(body, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        # Signing INSIDE the guarded path: with no fleet secret configured this
        # raises, the caller counts a failure, and the crawl continues — the
        # exact degradation the advance oracle already lives by.
        # THE SEAM THIS CLOSES. qe-central's /internal/pick-value routes
        # through _authenticate_internal, which computes
        # `tenant_scope("pick-value", claimed_tenant, crawl_id)`. The five
        # signers in main.py were migrated to that shape; this one was not,
        # so it kept signing the old `pick-value:<crawl_id>` and every value
        # consultation would have been refused 401 the moment the migration
        # landed. Left behind because this agent had no tenant to sign with;
        # it does now.
        signature = settings.sign_payload(
            payload,
            scope=tenant_scope("pick-value", self.tenant_id, self.crawl_id))
        self.calls += 1
        resp = self._client.post(
            settings.callback_url.rstrip("/") + "/internal/pick-value",
            content=payload,
            headers={"Content-Type": "application/json",
                     "X-QEC-Signature": signature,
                     "X-QEC-Token": settings.explorer_token},
        )
        if resp.status_code != 200:
            raise RuntimeError("http " + str(resp.status_code))
        data = resp.json()
        if str(data.get("status") or "") != "answered":
            return None
        return str(data.get("value") or "") or None

    # -- direct: a developer's bench only --------------------------------------
    def _ask_direct(self, **field_ctx: Any) -> Optional[str]:
        """Call the provider itself. REFUSES unless QEC_DATA_LLM_DIRECT=true.

        This is the path T-SEC-12 forbids in a deployment: no PII egress scan
        stands between a page's text and a third-party model. It exists so a
        developer can exercise rung 8 against a local fixture with no
        qe-central running, and for nothing else.
        """
        if os.environ.get("QEC_DATA_LLM_DIRECT", "").strip().lower() not in (
                "1", "true", "yes"):
            logger.warning(
                "qec.llm_data.direct_refused — direct provider calls bypass the "
                "PII egress guard; set QEC_DATA_LLM_DIRECT=true only on a dev "
                "bench, or configure the central route")
            return None
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            return None
        parts = [f"Field: {field_ctx.get('name') or '(unnamed)'}",
                 f"Kind: {field_ctx.get('kind') or 'text'}"]
        for label, k in (("Semantic type", "semantic_type"), ("Section", "section"),
                         ("Page", "page_title"), ("Constraints", "constraints")):
            v = str(field_ctx.get(k) or "").strip()
            if v:
                parts.append(f"{label}: {v}")
        options = list(field_ctx.get("options") or ())
        if options:
            parts.append("Allowed options (reply with one, verbatim): "
                         + " | ".join(str(o) for o in options[:40]))
        rejection = str(field_ctx.get("rejection") or "").strip()
        if rejection:
            parts.append("The application rejected the previous value with: "
                         + repr(rejection))
        self.calls += 1
        resp = self._client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": "Bearer " + key},
            json={"model": self.model, "temperature": 0.2, "max_tokens": 60,
                  "messages": [
                      {"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": "\n".join(parts)},
                  ]},
        )
        if resp.status_code != 200:
            raise RuntimeError("http " + str(resp.status_code))
        return (resp.json()["choices"][0]["message"]["content"] or "").strip()

    # -- resilience -------------------------------------------------------------
    def _note_failure(self, exc: Exception) -> None:
        self.failures_in_a_row += 1
        if self.failures_in_a_row >= self.breaker_threshold:
            self.breaker_open = True
            logger.warning("qec.llm_data.breaker_open failures=%d",
                           self.failures_in_a_row)
        else:
            logger.info("qec.llm_data.unavailable error=%s", str(exc)[:120])

    def stats(self) -> dict[str, Any]:
        return {"mode": self.mode, "calls": self.calls, "answered": self.answered,
                "breaker_open": self.breaker_open}

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:                                          # noqa: BLE001
            pass
