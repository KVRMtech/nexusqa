"""QE-Central — Phase-1 explorer-dispatch configuration + HMAC contract.

The Phase-0 ``app/config.py`` singleton is left untouched; the Phase-1 wiring
(dispatch OUT to the contained explorer + verify the explorer's completion
callback IN) adds its own env-driven ``pydantic_settings`` block here so the
bounded Phase-0 surface is not disturbed.

Security posture (design §1.1 — fail-closed everywhere):
  * The explorer holds NO DB creds / NO KMS — its only credential is the
    per-fleet HMAC shared secret ``QEC_EXPLORER_TOKEN`` (RUNNER_TOKEN pattern).
  * Dispatch sends that secret as the ``X-QEC-Token`` header.
  * The completion callback is authenticated by an HMAC-SHA256 signature over
    the RAW request body, carried in the ``X-QEC-Signature`` header.
  * :meth:`Phase1Settings.sign_payload` MIRRORS the explorer's
    ``app/config.py::Settings.sign_payload`` BYTE-FOR-BYTE (same raw-token key
    derivation, same digest) so the two subsystems agree; a divergence would
    reject every genuine callback.
  * An empty secret can NEVER verify — a mis-provisioned deployment refuses
    every callback rather than trusting an unsigned one.
"""
from __future__ import annotations

import hashlib
import hmac
import logging

from pydantic import Field
from pydantic_settings import BaseSettings

from ..security import hmac_auth
from ..security.hmac_auth import KeyRing, NonceStore, SignatureError

logger = logging.getLogger(__name__)

#: The header carrying the shared secret on dispatch (explorer verifies it via
#: its own ``settings.token_matches``).
TOKEN_HEADER = "X-QEC-Token"
#: The header carrying the v2 signature envelope over the raw callback body
#: (``v2;kid=…;ts=…;nonce=…;sig=…`` — see :mod:`app.security.hmac_auth`).
SIGNATURE_HEADER = "X-QEC-Signature"

#: Process-wide single-use nonce store for inbound explorer callbacks (T-SEC-06).
#: One store for the whole service: a nonce is single-use across EVERY internal
#: endpoint, so a signature captured from ``/pick-advance`` cannot be replayed at
#: ``/complete``.
CALLBACK_NONCES = NonceStore()


class Phase1Settings(BaseSettings):
    """Explorer-dispatch settings, loaded from the environment.

    Env-var names are pinned by the Phase-1 shared conventions; defaults are
    development-safe only and MUST be overridden by ``docker-compose.qec.yml``.
    """

    model_config = {"extra": "ignore", "populate_by_name": True}

    #: Base URL of the contained explorer on the internal ``qec-internal`` net.
    explorer_url: str = Field(
        default="http://qe-explorer:8210", alias="QEC_EXPLORER_URL",
    )
    #: Per-fleet HMAC shared secret (RUNNER_TOKEN pattern). Empty ⇒ fail-closed:
    #: dispatch and callback verification both refuse.
    explorer_token: str = Field(default="", alias="QEC_EXPLORER_TOKEN")
    #: ROTATION SEAM (T-SEC-11). The PREVIOUS fleet secret, still accepted for
    #: verification until ``explorer_token_previous_expires_at`` (epoch SECONDS)
    #: passes. Empty ⇒ no overlap window (today's single-key posture). A half-set
    #: rotation fails CLOSED: an absent/unparseable deadline retires the old key
    #: immediately rather than honouring it forever.
    explorer_token_previous: str = Field(
        default="", alias="QEC_EXPLORER_TOKEN_PREVIOUS",
    )
    explorer_token_previous_expires_at: float = Field(
        default=0.0, alias="QEC_EXPLORER_TOKEN_PREVIOUS_EXPIRES_AT",
    )
    #: Allowed clock skew (seconds, both directions) on a callback timestamp.
    #: Also the nonce-retention window — outside it the timestamp check already
    #: rejects, so forgetting a nonce past this bound cannot enable a replay.
    hmac_skew_seconds: float = Field(
        default=float(hmac_auth.DEFAULT_SKEW_SECONDS),
        alias="QEC_HMAC_SKEW_SECONDS",
    )
    #: Master switch for Phase-1 explorer dispatch. Default OFF keeps the
    #: Phase-0 inline-bundle path as the only active write path until an
    #: operator explicitly enables live crawling.
    dispatch_enabled: bool = Field(
        default=False, alias="QEC_EXPLORER_DISPATCH_ENABLED",
    )
    #: Caged exploration PLANNER — an LLM proposes grounded frontier-priority
    #: patterns for a RE-crawl (no-op on a first crawl: nothing seen to plan yet).
    #: Fail-open: LLM unavailable/ungrounded ⇒ empty plan ⇒ byte-identical crawl.
    planner_enabled: bool = Field(default=True, alias="QEC_PLANNER_ENABLED")
    #: qe-central-side mount of the shared ``qec-crawl-storage`` volume; the
    #: explorer writes ``{root}/{crawl_id}/manifest.jsonl`` + staged PNGs here.
    crawl_storage_root: str = Field(
        default="/work", alias="QEC_CRAWL_STORAGE_ROOT",
    )
    #: The squid allowlist file qe-central populates before each dispatch
    #: (shared ``qec-egress-allowlist`` volume). See squid.conf.
    egress_allowlist_path: str = Field(
        default="/qec/egress-allowlist/allowed_domains.txt",
        alias="QEC_EGRESS_ALLOWLIST_PATH",
    )
    #: OPTIONAL pool of explorer WORKERS for concurrent crawls. A JSON array of
    #: ``{"url": ..., "allowlist_path": ...}``. CRITICAL: each worker MUST have its
    #: OWN squid egress allowlist file (per-worker isolation) — a shared allowlist
    #: would be raced/clobbered by concurrent crawls and BREAK the egress fence, so
    #: each entry pins the file qe-central writes before dispatching THAT worker.
    #: Empty (the default) ⇒ exactly the single worker (``explorer_url`` +
    #: ``egress_allowlist_path``) above — byte-identical to the pre-pool behavior.
    explorer_pool: str = Field(default="", alias="QEC_EXPLORER_POOL")

    def workers(self) -> list[dict]:
        """The explorer worker pool as ``[{"url", "allowlist_path"}, ...]``.

        Parses ``QEC_EXPLORER_POOL`` (JSON); a malformed/empty value falls back to
        the single ``(explorer_url, egress_allowlist_path)`` worker — so an
        unconfigured or mis-set pool is byte-identical to today, never fail-open.
        Every returned worker has a non-empty url AND its own allowlist_path.
        """
        raw = (self.explorer_pool or "").strip()
        if raw:
            try:
                import json
                out: list[dict] = []
                for item in json.loads(raw):
                    url = str((item or {}).get("url") or "").strip()
                    ap = str((item or {}).get("allowlist_path") or "").strip()
                    if url and ap:
                        out.append({"url": url, "allowlist_path": ap})
                if out:
                    return out
            except Exception:
                pass  # malformed pool → fall back to the single worker (never fail-open)
        return [{"url": self.explorer_url, "allowlist_path": self.egress_allowlist_path}]
    #: httpx dispatch timeout (seconds).
    dispatch_timeout_s: float = Field(
        default=30.0, alias="QEC_EXPLORER_DISPATCH_TIMEOUT_S",
    )
    #: httpx timeout for the E3 auth-import relay (seconds).
    auth_import_timeout_s: float = Field(
        default=30.0, alias="QEC_AUTH_IMPORT_TIMEOUT_S",
    )

    # ── HMAC helpers (MIRROR of the explorer sign contract) ───────────────

    def keyring(self) -> KeyRing:
        """The keys this fleet will ACCEPT right now (current + overlap)."""
        return KeyRing(
            current=self.explorer_token or "",
            previous=self.explorer_token_previous or "",
            previous_expires_at=float(self.explorer_token_previous_expires_at or 0.0),
        )

    def sign_payload(self, payload: bytes, *, scope: str = "") -> str:
        """The v2 ``X-QEC-Signature`` envelope for ``payload``.

        MIRRORS the explorer's ``Settings.sign_payload`` byte-for-byte (both
        delegate to the duplicated :mod:`hmac_auth` module); a divergence would
        reject every genuine callback.  ``scope`` binds the signature to one
        logical operation so a signature for crawl A cannot authenticate a call
        about crawl B.
        """
        return hmac_auth.sign(payload, keyring=self.keyring(), scope=scope)

    def verify_callback(
        self, payload: bytes, provided: str | None, *, scope: str = "",
        now: float | None = None,
    ) -> dict:
        """Verify an inbound callback signature; RAISE ``SignatureError`` if not.

        Enforces, fail-closed and in this order: known key id → timestamp within
        skew (past AND future) → nonce unused → signature over the body hash +
        scope → nonce consumed.  An unsigned, stale, replayed, re-scoped or
        wrong-key callback is never trusted.
        """
        if not (self.explorer_token or "").strip():
            raise SignatureError("no_verification_key")
        return hmac_auth.verify(
            payload, provided or "", keyring=self.keyring(),
            nonces=CALLBACK_NONCES, scope=scope,
            skew_seconds=float(self.hmac_skew_seconds), now=now,
        )

    def verify_signature(
        self, payload: bytes, provided: str | None, *, scope: str = "",
    ) -> bool:
        """Boolean form of :meth:`verify_callback` (fail-closed).

        Returns ``False`` — never raises — for every rejection category, and
        logs the CATEGORY (never the key, nonce or body) so an operator can tell
        a replay from a clock-skew problem from a retired key.
        """
        try:
            self.verify_callback(payload, provided, scope=scope)
            return True
        except SignatureError as exc:
            logger.warning(
                "qec.hmac.callback_rejected reason=%s scope=%s", exc.reason, scope,
            )
            return False

    def token_value(self) -> str:
        """The ``X-QEC-Token`` value to send on dispatch (stripped)."""
        return (self.explorer_token or "").strip()


#: Singleton — import as ``from ..clients.config import phase1_settings``.
phase1_settings = Phase1Settings()
